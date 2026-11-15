"""Probabilistic reasoning with a small Bayesian network.

Network structure (per shelter s):

        rain (high / moderate / low)
       /    \\                          \\
 route_blocked_s   damage_s          demand_surge
       \\      /                          (regional)
     accessible_s = NOT blocked AND NOT damaged

Exact inference by enumeration over the joint (the net is tiny, so exact
posterior probabilities are computed - no approximation needed).

Inputs  : evidence dict, e.g. {"rain": "high", "route_blocked_S3": true,
                               "damage_S2": false}
Outputs : per-shelter P(accessible) before/after evidence, P(demand surge),
          risk-adjusted capacities and a risk-aware allocation plan built
          with the greedy allocator on shrunken capacities.
"""
from __future__ import annotations

import math

from .search import greedy, evaluate


class BayesNode:
    def __init__(self, name, values, parents, cpt):
        self.name = name
        self.values = values                  # list of possible values
        self.parents = parents                # list of parent names
        self.cpt = cpt                        # {parent_value_tuple: {value: p}}


# ----------------------------------------------------------------------
def build_network(scenario):
    nodes = {}
    nodes["rain"] = BayesNode("rain", ["high", "moderate", "low"], [], {
        (): {"high": 0.35, "moderate": 0.45, "low": 0.20},
    })
    for s in scenario.shelters:
        nodes[f"route_blocked_{s.id}"] = BayesNode(
            f"route_blocked_{s.id}", [True, False], ["rain"], {
                ("high",): {True: 0.55, False: 0.45},
                ("moderate",): {True: 0.25, False: 0.75},
                ("low",): {True: 0.06, False: 0.94},
            })
        nodes[f"damage_{s.id}"] = BayesNode(
            f"damage_{s.id}", [True, False], ["rain"], {
                ("high",): {True: 0.20, False: 0.80},
                ("moderate",): {True: 0.10, False: 0.90},
                ("low",): {True: 0.04, False: 0.96},
            })
        nodes[f"accessible_{s.id}"] = BayesNode(
            f"accessible_{s.id}", [True, False],
            [f"route_blocked_{s.id}", f"damage_{s.id}"], {
                (True, True): {True: 0.00, False: 1.00},
                (True, False): {True: 0.00, False: 1.00},
                (False, True): {True: 0.00, False: 1.00},
                (False, False): {True: 0.98, False: 0.02},
            })
    nodes["demand_surge"] = BayesNode("demand_surge", [True, False], ["rain"], {
        ("high",): {True: 0.60, False: 0.40},
        ("moderate",): {True: 0.30, False: 0.70},
        ("low",): {True: 0.10, False: 0.90},
    })
    return nodes


def _topo_order(nodes):
    """Parents always precede children (net is built in order; this guards it)."""
    done, order = set(), []
    def visit(name):
        if name in done:
            return
        for p in nodes[name].parents:
            visit(p)
        done.add(name)
        order.append(name)
    for name in nodes:
        visit(name)
    return order


def query(nodes, target, evidence) -> dict:
    """Exact posterior P(target | evidence) by enumeration."""
    order = _topo_order(nodes)
    dist = {v: 0.0 for v in nodes[target].values}
    assignment = {}

    def walk(i, p, tval):
        if p <= 0.0:
            return
        if i == len(order):
            if tval is not None:
                dist[tval] += p
            return
        name = order[i]
        node = nodes[name]
        parent_vals = tuple(assignment[pn] for pn in node.parents)
        if name in evidence:
            v = evidence[name]
            assignment[name] = v
            walk(i + 1, p * node.cpt.get(parent_vals, {}).get(v, 0.0),
                 v if name == target else tval)
            del assignment[name]
        else:
            for v in node.values:
                assignment[name] = v
                walk(i + 1, p * node.cpt[parent_vals][v],
                     v if name == target else tval)
                del assignment[name]

    walk(0, 1.0, None)
    total = sum(dist.values()) or 1.0
    return {v: p / total for v, p in dist.items()}


# ----------------------------------------------------------------------
def analyze(scenario, evidence: dict | None = None) -> dict:
    """Full probabilistic analysis + a risk-aware allocation plan."""
    evidence = dict(evidence or {})
    # Normalise evidence keys/values (accept "unknown"/"" as absent)
    clean = {}
    for k, v in evidence.items():
        if v in (None, "", "unknown"):
            continue
        if isinstance(v, str):
            v = {"yes": True, "no": False, "true": True, "false": False}.get(v.lower(), v)
        clean[k] = v
    evidence = clean

    nodes = build_network(scenario)
    shelters = []
    for s in scenario.shelters:
        prior = query(nodes, f"accessible_{s.id}", {})
        post = query(nodes, f"accessible_{s.id}", evidence)
        p = post.get(True, 0.0)
        # Capacity is reduced by the chance the BUILDING is unusable
        # (damage); route problems are handled separately via `risky`
        # (a blocked route means rerouting, not losing the beds).
        p_usable = query(nodes, f"damage_{s.id}", evidence).get(False, 0.0)
        risk_cap = int(math.floor(s.capacity * p_usable / 1.10))  # 10% margin
        shelters.append({
            "shelter": s.id,
            "name": s.name,
            "capacity": s.capacity,
            "p_accessible_prior": round(prior.get(True, 0.0), 3),
            "p_accessible": round(p, 3),
            "risk_adjusted_capacity": max(0, risk_cap),
        })
    surge = round(query(nodes, "demand_surge", evidence).get(True, 0.0), 3)
    rain_post = query(nodes, "rain", evidence)

    # Risk-aware plan: strict greedy on damage-adjusted capacities; shelters
    # whose route is probably cut off (P(accessible) < 20%) are used only as
    # a flagged last resort.
    adjusted_caps = {sh["shelter"]: sh["risk_adjusted_capacity"] for sh in shelters}
    risky = {sh["shelter"]: sh["p_accessible"] for sh in shelters
             if sh["p_accessible"] < 0.2}
    plan = greedy(scenario, capacities=adjusted_caps, risky=risky)
    metrics = evaluate(scenario, plan["allocation"])
    risky_people = sum(z.population for z in scenario.zones
                       if plan["allocation"].get(z.id) in risky)

    return {
        "evidence": evidence,
        "rain_posterior": {k: round(v, 3) for k, v in rain_post.items()},
        "demand_surge_probability": surge,
        "shelters": shelters,
        "risk_adjusted_capacities": adjusted_caps,
        "risky_shelters": risky,
        "risky_people": risky_people,
        "risk_aware_plan": plan,
        "risk_aware_metrics": metrics,
        "narrative": _narrative(scenario, shelters, surge, evidence, metrics,
                                risky_people),
    }


def _narrative(scenario, shelters, surge, evidence, metrics, risky_people) -> list:
    lines = []
    ev = ", ".join(f"{k}={v}" for k, v in evidence.items()) or "no evidence (priors only)"
    lines.append(f"Bayesian inference given: {ev}.")
    riskiest = min(shelters, key=lambda x: x["p_accessible"])
    lines.append(f"Least reliable shelter: {riskiest['name']} "
                 f"(P(accessible) = {riskiest['p_accessible']:.0%}) - plan for "
                 f"{riskiest['risk_adjusted_capacity']} of {riskiest['capacity']} places.")
    if surge >= 0.5:
        lines.append(f"High demand-surge risk ({surge:.0%}) - expect walk-in evacuees "
                     f"beyond registered zone populations; keep reserve capacity.")
    elif surge >= 0.25:
        lines.append(f"Moderate demand-surge risk ({surge:.0%}); monitor arrivals hourly.")
    else:
        lines.append(f"Low demand-surge risk ({surge:.0%}).")
    if risky_people > 0:
        lines.append(f"{risky_people} people are routed through shelters whose route is "
                     f"probably blocked (flagged RISKY in the plan) - pre-position "
                     f"boat/high-clearance transport or request mutual aid.")
    if metrics["coverage_pct"] < 100:
        lines.append(f"Risk-aware plan covers {metrics['coverage_pct']}% of evacuees "
                     f"({metrics['unallocated_people']} people unplaced) - the price of "
                     f"planning against unreliable routes.")
    return lines
