"""Multi-agent coordination: contract-net style negotiation.

Agents:
  CoordinatorAgent - runs the protocol, broadcasts calls for proposals,
                      awards contracts, escalates failures
  ZoneAgent (1/zone)       - announces its evacuee need (population,
                             priority, medical requirement)
  ShelterAgent (1/shelter) - bids for zones it can absorb (scored on
                             distance, free capacity, suitability)
  TransportAgent           - schedules convoys for awarded contracts

The negotiation is deterministic and fully logged, so the UI can replay
the coordination step by step. The resulting allocation is scored with
the same evaluate() as every other approach.
"""
from __future__ import annotations

import math

from .search import evaluate

CONVOY_SIZE = 120      # people per convoy
CONVOY_SPEED_KMH = 40  # average convoy speed


def run_agents(scenario) -> dict:
    log = []
    clock = {"min": 0}

    def add(agent, action, detail, atype="msg"):
        log.append({
            "step": len(log) + 1,
            "time_min": clock["min"],
            "agent": agent,
            "action": action,
            "type": atype,
            "detail": detail,
        })

    loads = {s.id: 0 for s in scenario.shelters}
    allocation = {}
    zones = sorted(scenario.zones, key=lambda z: (-z.priority, -z.population))

    add("CoordinatorAgent", "ACTIVATE",
        f"Disaster response started for '{scenario.name}' ({scenario.disaster_type}). "
        f"{len(zones)} affected zones, {len(scenario.shelters)} shelters online.")
    escalations = 0

    for z in zones:
        clock["min"] += 2
        medical_note = ", medical unit REQUIRED" if z.medical_need else ""
        add(f"ZoneAgent:{z.id}", "ANNOUNCE",
            f"'{z.name}' requests shelter for {z.population} evacuees "
            f"(priority {z.priority}/5{medical_note}).")

        add("CoordinatorAgent", "CFP",
            f"Call-for-proposals broadcast to {len(scenario.shelters)} shelter agents.")

        bids = []
        for s in scenario.shelters:
            free = s.capacity - loads[s.id]
            med_ok = (not z.medical_need) or s.medical
            room = free >= z.population
            if not room:
                continue
            d = scenario.distance(z.id, s.id)
            util = loads[s.id] / max(1, s.capacity)
            if med_ok:
                score = (0.5 * (1 - min(d, scenario.max_travel_km)
                                / max(scenario.max_travel_km, 0.01))
                         + 0.3 * (1 - util) + 0.2)
                bids.append({"shelter": s, "score": score, "d": d, "relaxed": False})
            else:
                # Shelter has room but no medical unit: bid only with a penalty
                # (medical-needed zones can still fall back here if nothing else fits).
                score = (0.5 * (1 - min(d, scenario.max_travel_km)
                                / max(scenario.max_travel_km, 0.01))
                         + 0.2 * (1 - util) - 0.2)
                bids.append({"shelter": s, "score": score, "d": d, "relaxed": True})

        for b in bids:
            tag = "BID" if not b["relaxed"] else "BID (medical relaxed)"
            add(f"ShelterAgent:{b['shelter'].id}", tag,
                f"offers placement: score {b['score']:.2f}, distance {b['d']} km, "
                f"{b['shelter'].capacity - loads[b['shelter'].id]} places free.")

        if bids:
            best = max(bids, key=lambda b: b["score"])
            add("CoordinatorAgent", "AWARD",
                f"Zone {z.id} ('{z.name}') awarded to {best['shelter'].id} "
                f"('{best['shelter'].name}').")
            loads[best["shelter"].id] += z.population
            allocation[z.id] = best["shelter"].id
            convoys = math.ceil(z.population / CONVOY_SIZE)
            eta = int(round(best["d"] / CONVOY_SPEED_KMH * 60))
            clock["min"] += 3
            add("TransportAgent", "SCHEDULE",
                f"{convoys} convoy(s) dispatched {z.id} -> {best['shelter'].id}, "
                f"ETA {eta} min.")
        else:
            escalations += 1
            add("CoordinatorAgent", "ESCALATE",
                f"No shelter can absorb zone {z.id} ('{z.name}', {z.population} people). "
                f"Requesting external relief camp / mutual aid.", atype="alert")

    metrics = evaluate(scenario, allocation)
    clock["min"] += 2
    add("CoordinatorAgent", "SUMMARY",
        f"Coordination complete: {metrics['allocated_people']} people placed "
        f"({metrics['coverage_pct']}% coverage), {escalations} escalation(s).")

    roster = [{
        "shelter": s.id,
        "name": s.name,
        "load": loads[s.id],
        "capacity": s.capacity,
        "utilization": round(loads[s.id] / max(1, s.capacity), 3),
    } for s in scenario.shelters]

    return {
        "log": log,
        "allocation": allocation,
        "metrics": metrics,
        "messages": len(log),
        "escalations": escalations,
        "shelter_roster": roster,
        "completion_time_min": clock["min"],
    }
