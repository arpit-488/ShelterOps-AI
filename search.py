"""Classical search over allocation states.

An *allocation state* maps every zone to one shelter (or None = not yet
placed). This module implements:

- evaluate()            : scores any allocation (cost + constraint penalties)
- greedy()              : priority-first nearest-feasible-shelter baseline
- backtracking()        : branch & bound systematic search (optimal for
                          small instances), with a search trace
- hill_climbing()       : local search baseline for the comparison tab

Hard constraints are expressed as big linear penalties so every algorithm
can be compared on one scale:
    unallocated people, capacity overflow, medical mismatch, distance limit.
"""
from __future__ import annotations

import random

# Penalty per person for breaking each soft-reported constraint.
P_UNALLOCATED = 200.0
P_OVERFLOW = 200.0
P_MEDICAL = 150.0
P_DISTANCE = 60.0


def priority_weight(priority: int) -> float:
    """Vulnerable zones weigh more: priority 5 travels at weight 2.0."""
    return 1.0 + 0.25 * (priority - 1)


# ----------------------------------------------------------------------
# Evaluation of an allocation state
# ----------------------------------------------------------------------
def evaluate(scenario, allocation: dict) -> dict:
    """Compute metrics for {zone_id: shelter_id | None}."""
    loads = {s.id: 0 for s in scenario.shelters}
    cost = 0.0
    dist_sum, dist_n = 0.0, 0
    allocated_zones, allocated_people, unallocated_people = 0, 0, 0
    overflow, med_viol, dist_viol = 0, 0, 0

    for z in scenario.zones:
        sid = allocation.get(z.id)
        if sid is None or sid not in loads:
            unallocated_people += z.population
            continue
        s = scenario.shelter(sid)
        loads[sid] += z.population
        allocated_zones += 1
        allocated_people += z.population
        d = scenario.distance(z.id, sid)
        cost += z.population * d * priority_weight(z.priority)
        dist_sum += d
        dist_n += 1
        if z.medical_need and not s.medical:
            med_viol += z.population
        if d > scenario.max_travel_km:
            dist_viol += z.population

    for sid, load in loads.items():
        cap = scenario.shelter(sid).capacity
        if load > cap:
            overflow += load - cap

    penalized = (cost
                 + P_UNALLOCATED * unallocated_people
                 + P_OVERFLOW * overflow
                 + P_MEDICAL * med_viol
                 + P_DISTANCE * dist_viol)
    total_pop = max(1, scenario.total_population)
    utilization = {
        sid: round(load / max(1, scenario.shelter(sid).capacity), 3)
        for sid, load in loads.items()
    }
    return {
        "allocation": dict(allocation),
        "weighted_cost": round(cost, 1),
        "penalized_score": round(penalized, 1),
        "allocated_zones": allocated_zones,
        "allocated_people": allocated_people,
        "unallocated_people": unallocated_people,
        "coverage_pct": round(100.0 * allocated_people / total_pop, 1),
        "overflow_people": overflow,
        "medical_violations_people": med_viol,
        "distance_violations_people": dist_viol,
        "avg_distance_km": round(dist_sum / dist_n, 2) if dist_n else 0.0,
        "shelter_loads": loads,
        "utilization": utilization,
        "max_utilization": round(max(utilization.values()), 3) if utilization else 0.0,
        "feasible": (unallocated_people == 0 and overflow == 0
                     and med_viol == 0 and dist_viol == 0),
    }


def penalized(scenario, allocation: dict) -> float:
    return evaluate(scenario, allocation)["penalized_score"]


# ----------------------------------------------------------------------
# 1) Greedy - priority-first, nearest feasible shelter, with relaxation ladder
# ----------------------------------------------------------------------
def greedy(scenario, capacities: dict | None = None, strict: bool | None = None,
           risky: dict | None = None) -> dict:
    """Assign the most vulnerable zones first to their nearest shelter that
    still has room. If nothing fits, relax constraints one at a time so the
    zone is never silently dropped (each relaxation is recorded in steps).

    `capacities` optionally overrides shelter capacity (used by the Bayesian
    risk-aware plan), which implies strict mode: no overflow/medical
    relaxation. `risky` is a dict {shelter_id: P(accessible)} of shelters a
    coordinator would rather NOT use (route likely blocked); they are tried
    only as a flagged last resort, so coverage survives while the risk is
    made explicit in the step notes.
    """
    if strict is None:
        strict = capacities is not None
    risky = risky or {}
    caps = {s.id: (capacities or {}).get(s.id, s.capacity) for s in scenario.shelters}
    loads = {s.id: 0 for s in scenario.shelters}
    allocation, steps = {}, []
    zones = sorted(scenario.zones, key=lambda z: (-z.priority, -z.population))

    for z in zones:
        chosen, note = None, "no shelter could absorb the zone - escalation needed"

        def room_med(s, z=z):
            return loads[s.id] + z.population <= caps[s.id] and (not z.medical_need or s.medical)

        def room(s, z=z):
            return loads[s.id] + z.population <= caps[s.id]

        safe = [s for s in scenario.shelters if s.id not in risky]
        passes = [
            ("room + medical ok", room_med, safe),
            ("room (medical relaxed)", room, safe),
        ]
        if strict:
            if risky:
                passes += [
                    ("RISKY last resort: room + medical ok (route likely blocked)",
                     room_med, scenario.shelters),
                    ("RISKY last resort: room (route likely blocked)", room,
                     scenario.shelters),
                ]
        else:
            passes += [
                ("medical ok (overflow risk)", lambda s: not z.medical_need or s.medical,
                 scenario.shelters),
                ("any shelter (overflow risk)", lambda s: True, scenario.shelters),
            ]

        for label, pred, pool in passes:
            cands = [s for s in pool if pred(s)]
            if cands:
                best = min(cands, key=lambda s: scenario.distance(z.id, s.id) * priority_weight(z.priority))
                chosen = best.id
                p = risky.get(best.id)
                note = (f"pass: {label} (P(accessible)={p:.0%})"
                        if p is not None else f"pass: {label}")
                break
        if chosen is not None:
            loads[chosen] += z.population
        allocation[z.id] = chosen
        steps.append({
            "zone": z.id,
            "shelter": chosen,
            "distance": scenario.distance(z.id, chosen) if chosen else None,
            "note": note,
        })
    return {"allocation": allocation, "steps": steps}


# ----------------------------------------------------------------------
# 2) Backtracking with branch & bound - systematic, optimal (small instances)
# ----------------------------------------------------------------------
TRACE_LIMIT = 200


def backtracking(scenario, node_limit: int = 300_000) -> dict:
    """Depth-first search over assignment trees with:
       - priority-ordered variables (critical zones decided first),
       - distance-ordered shelter domains (good solutions found early),
       - an admissible lower bound (min possible cost per remaining zone)
         used to prune branches worse than the best complete solution.

    Returns the optimal (or node-limited best) allocation plus a trace of
    assignments/prunes for the UI search visualiser.
    """
    zones = sorted(scenario.zones, key=lambda z: (-z.priority, -z.population))
    m = len(zones)
    caps = {s.id: s.capacity for s in scenario.shelters}

    def zone_cost(z, sid):
        if sid is None:
            return P_UNALLOCATED * z.population
        s = scenario.shelter(sid)
        d = scenario.distance(z.id, sid)
        c = z.population * d * priority_weight(z.priority)
        if z.medical_need and not s.medical:
            c += P_MEDICAL * z.population
        if d > scenario.max_travel_km:
            c += P_DISTANCE * z.population
        return c

    # Admissible lower bound: cheapest imaginable placement per zone (incl. None).
    min_cost = {}
    for z in zones:
        opts = [zone_cost(z, s.id) for s in scenario.shelters] + [zone_cost(z, None)]
        min_cost[z.id] = min(opts)
    suffix = [0.0] * (m + 1)
    for i in range(m - 1, -1, -1):
        suffix[i] = suffix[i + 1] + min_cost[zones[i].id]

    best_score, best_alloc = float("inf"), None
    loads = {s.id: 0 for s in scenario.shelters}
    alloc, trace = {}, []
    nodes, truncated = 0, False

    def record(event):
        if len(trace) < TRACE_LIMIT:
            trace.append(event)

    def leaf_score(running):
        overflow = sum(max(0, loads[sid] - caps[sid]) for sid in caps)
        return running + P_OVERFLOW * overflow

    def dfs(i, running):
        nonlocal nodes, best_score, best_alloc, truncated
        nodes += 1
        if nodes > node_limit:
            truncated = True
            return
        if i == m:
            score = leaf_score(running)
            if score < best_score:
                best_score, best_alloc = score, dict(alloc)
                record({"depth": i, "event": "complete",
                        "score": round(score, 1), "note": "new best complete solution"})
            return
        if running + suffix[i] >= best_score:
            if nodes % 97 == 1:
                record({"depth": i, "event": "prune",
                        "score": round(running + suffix[i], 1),
                        "note": f"branch pruned: bound >= best {round(best_score, 1)}"})
            return

        z = zones[i]
        eligible = [s for s in scenario.shelters if (not z.medical_need) or s.medical]
        others = [s for s in scenario.shelters if s not in eligible]
        eligible.sort(key=lambda s: scenario.distance(z.id, s.id))
        others.sort(key=lambda s: scenario.distance(z.id, s.id))
        for s in eligible + others:
            alloc[z.id] = s.id
            loads[s.id] += z.population
            if i <= 2 and len(trace) < TRACE_LIMIT:
                record({"depth": i, "event": "assign",
                        "note": f"{z.id} -> {s.id} (d={scenario.distance(z.id, s.id)} km, "
                                f"load {loads[s.id]}/{caps[s.id]})"})
            dfs(i + 1, running + zone_cost(z, s.id))
            loads[s.id] -= z.population
            del alloc[z.id]
            if truncated:
                return
        # Option: leave the zone unallocated (always considered, heavily penalised)
        alloc[z.id] = None
        dfs(i + 1, running + zone_cost(z, None))
        del alloc[z.id]

    dfs(0, 0.0)
    if best_alloc is None:
        best_alloc = {z.id: None for z in zones}
    return {
        "allocation": best_alloc,
        "nodes": nodes,
        "trace": trace,
        "truncated": truncated,
        "best_score": round(best_score, 1),
    }


# ----------------------------------------------------------------------
# 3) Hill climbing - simplest local search baseline
# ----------------------------------------------------------------------
def hill_climbing(scenario, max_evals: int = 3000, seed: int = 42) -> dict:
    """Start from a random complete assignment, repeatedly move one zone to
    another shelter whenever it lowers the penalised score. Stops at a local
    optimum (or when the evaluation budget runs out)."""
    rng = random.Random(seed)
    sids = [s.id for s in scenario.shelters]
    zids = [z.id for z in scenario.zones]

    current = {z: rng.choice(sids) for z in zids}
    cur = penalized(scenario, current)
    curve, evals = [round(cur, 1)], 1
    improved = True
    while improved and evals < max_evals:
        improved = False
        for zid in zids:
            for sid in sids:
                if sid == current[zid]:
                    continue
                cand = dict(current)
                cand[zid] = sid
                c = penalized(scenario, cand)
                evals += 1
                if c < cur - 1e-9:
                    current, cur = cand, c
                    improved = True
                if evals >= max_evals:
                    break
            if evals >= max_evals:
                break
        curve.append(round(cur, 1))
    return {"allocation": current, "curve": curve, "evals": evals}
