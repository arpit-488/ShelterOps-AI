"""Rule-based logical reasoning about an allocation.

Implements forward chaining over ground atoms (Datalog style, closed-world
assumption). Facts are tuples like ("needs_medical", "Z1"); rules are Horn
clauses whose bodies are predicates over the fact set. Chaining loops until
a fixpoint, recording every rule that fires - that record is what the UI
shows as "logical reasoning in action".

Rules (logical form shown in the UI):
  R1  needs_medical(z)  AND has_medical(s)                    -> med_ok(z, s)
  R2  distance(z, s, d) AND d <= max_travel                   -> reachable(z, s)
  R3  reachable(z, s) AND (NOT needs_medical(z) OR med_ok(z,s))-> candidate(z, s)
  R4  load(s, n) AND capacity(s, c) AND n <= c                -> has_room(s)
      load(s, n) AND capacity(s, c) AND n >  c                -> overcrowded(s)
  R5  allocated(z, s) AND overcrowded(s)                      -> capacity_violation(z, s)
  R6  allocated(z, s) AND needs_medical(z) AND NOT has_medical(s) -> medical_violation(z, s)
  R7  allocated(z, s) AND NOT reachable(z, s)                 -> distance_violation(z, s)
  R8  NOT EXISTS s: allocated(z, s)                           -> unallocated(z)
  R9  unallocated(z) AND high_priority(z)                     -> urgent_unallocated(z)
"""
from __future__ import annotations

from collections import namedtuple

Rule = namedtuple("Rule", "name form body")


def pretty(fact: tuple) -> str:
    return f"{fact[0]}({', '.join(str(a) for a in fact[1:])})"


# ----------------------------------------------------------------------
def analyze(scenario, allocation: dict | None = None) -> dict:
    """Build facts from the scenario (+ optional allocation), forward chain
    to a fixpoint, and return facts, fired rules and violations."""
    allocation = allocation or {}
    facts: set = set()
    for z in scenario.zones:
        facts.add(("zone", z.id, z.name))
        facts.add(("population", z.id, z.population))
        facts.add(("priority", z.id, z.priority))
        if z.priority >= 4:
            facts.add(("high_priority", z.id))
        if z.medical_need:
            facts.add(("needs_medical", z.id))
    for s in scenario.shelters:
        facts.add(("shelter", s.id, s.name))
        facts.add(("capacity", s.id, s.capacity))
        if s.medical:
            facts.add(("has_medical", s.id))
    for z in scenario.zones:
        for s in scenario.shelters:
            facts.add(("distance", z.id, s.id, scenario.distance(z.id, s.id)))

    loads = {s.id: 0 for s in scenario.shelters}
    for z in scenario.zones:
        sid = allocation.get(z.id)
        if sid is not None and sid in loads:
            facts.add(("allocated", z.id, sid))
            loads[sid] += z.population
    for sid, load in loads.items():
        if load:
            facts.add(("load", sid, load))

    names = {
        "zone_names": {z.id: z.name for z in scenario.zones},
        "shelter_names": {s.id: s.name for s in scenario.shelters},
        "caps": {s.id: s.capacity for s in scenario.shelters},
        "loads": loads,
        "max_travel": scenario.max_travel_km,
        "zone_ids": [z.id for z in scenario.zones],
    }

    rules = _build_rules(names)
    initial_count = len(facts)

    fired = []
    changed = True
    while changed:
        changed = False
        for rule in rules:
            derived = rule.body(facts, names)
            fresh = derived - facts
            if fresh:
                facts |= fresh
                fired.append({
                    "rule": rule.name,
                    "form": rule.form,
                    "count": len(fresh),
                    "derived": [pretty(f) for f in
                                sorted(fresh, key=pretty)[:6]],
                })
                changed = True

    return _summarise(facts, fired, names, initial_count)


# ----------------------------------------------------------------------
def _build_rules(names) -> list:
    max_travel = names["max_travel"]

    def r1(facts, ctx):
        med_z = {f[1] for f in facts if f[0] == "needs_medical"}
        med_s = {f[1] for f in facts if f[0] == "has_medical"}
        return {("med_ok", z, s) for z in med_z for s in med_s}

    def r2(facts, ctx):
        return {("reachable", f[1], f[2]) for f in facts
                if f[0] == "distance" and f[3] <= max_travel}

    def r3(facts, ctx):
        needs = {f[1] for f in facts if f[0] == "needs_medical"}
        reach = {(f[1], f[2]) for f in facts if f[0] == "reachable"}
        medok = {(f[1], f[2]) for f in facts if f[0] == "med_ok"}
        return {("candidate", z, s) for (z, s) in reach
                if z not in needs or (z, s) in medok}

    def r4(facts, ctx):
        out = set()
        for f in facts:
            if f[0] != "load":
                continue
            sid, load = f[1], f[2]
            cap = ctx["caps"].get(sid, 0)
            out.add(("has_room", sid) if load <= cap else ("overcrowded", sid))
        return out

    def r5(facts, ctx):
        crowded = {f[1] for f in facts if f[0] == "overcrowded"}
        return {("capacity_violation", f[1], f[2]) for f in facts
                if f[0] == "allocated" and f[2] in crowded}

    def r6(facts, ctx):
        med_s = {f[1] for f in facts if f[0] == "has_medical"}
        return {("medical_violation", f[1], f[2]) for f in facts
                if f[0] == "allocated" and f[1] in
                {g[1] for g in facts if g[0] == "needs_medical"}
                and f[2] not in med_s}

    def r7(facts, ctx):
        reach = {(f[1], f[2]) for f in facts if f[0] == "reachable"}
        return {("distance_violation", f[1], f[2]) for f in facts
                if f[0] == "allocated" and (f[1], f[2]) not in reach}

    def r8(facts, ctx):
        placed = {f[1] for f in facts if f[0] == "allocated"}
        return {("unallocated", z) for z in ctx["zone_ids"] if z not in placed}

    def r9(facts, ctx):
        unalloc = {f[1] for f in facts if f[0] == "unallocated"}
        high = {f[1] for f in facts if f[0] == "high_priority"}
        return {("urgent_unallocated", z) for z in unalloc & high}

    return [
        Rule("R1 medical-eligibility",
             "needs_medical(z) AND has_medical(s) -> med_ok(z,s)", r1),
        Rule("R2 reachability",
             "distance(z,s,d) AND d <= max_travel -> reachable(z,s)", r2),
        Rule("R3 candidate-shelter",
             "reachable(z,s) AND (NOT needs_medical(z) OR med_ok(z,s)) -> candidate(z,s)", r3),
        Rule("R4 room-check",
             "load(s,n) AND capacity(s,c) AND n <= c -> has_room(s); n > c -> overcrowded(s)", r4),
        Rule("R5 capacity-violation",
             "allocated(z,s) AND overcrowded(s) -> capacity_violation(z,s)", r5),
        Rule("R6 medical-violation",
             "allocated(z,s) AND needs_medical(z) AND NOT has_medical(s) -> medical_violation(z,s)", r6),
        Rule("R7 distance-violation",
             "allocated(z,s) AND NOT reachable(z,s) -> distance_violation(z,s)", r7),
        Rule("R8 unallocated",
             "NOT EXISTS s allocated(z,s) -> unallocated(z)", r8),
        Rule("R9 urgent-unallocated",
             "unallocated(z) AND high_priority(z) -> urgent_unallocated(z)", r9),
    ]


# ----------------------------------------------------------------------
def _summarise(facts, fired, names, initial_count) -> dict:
    zn, sn = names["zone_names"], names["shelter_names"]
    violations = []
    for f in sorted(facts, key=pretty):
        if f[0] == "capacity_violation":
            violations.append({"type": "capacity",
                               "zone": f[1], "shelter": f[2],
                               "message": f"{zn.get(f[1], f[1])} placed at "
                                          f"{sn.get(f[2], f[2])} but the shelter is over capacity"})
        elif f[0] == "medical_violation":
            violations.append({"type": "medical",
                               "zone": f[1], "shelter": f[2],
                               "message": f"{zn.get(f[1], f[1])} needs medical care but "
                                          f"{sn.get(f[2], f[2])} has no medical unit"})
        elif f[0] == "distance_violation":
            violations.append({"type": "distance",
                               "zone": f[1], "shelter": f[2],
                               "message": f"{zn.get(f[1], f[1])} -> {sn.get(f[2], f[2])} "
                                          f"exceeds the max travel distance"})
    unallocated = sorted({f[1] for f in facts if f[0] == "unallocated"})
    urgent = sorted({f[1] for f in facts if f[0] == "urgent_unallocated"})

    verdict = "FEASIBLE"
    if violations or unallocated:
        verdict = f"ISSUES FOUND: {len(violations)} violation(s), {len(unallocated)} unallocated zone(s)"

    return {
        "initial_fact_count": initial_count,
        "total_facts": len(facts),
        "facts": [pretty(f) for f in sorted(facts, key=pretty)],
        "fired_rules": fired,
        "violations": violations,
        "unallocated_zones": unallocated,
        "urgent_unallocated_zones": urgent,
        "verdict": verdict,
    }
