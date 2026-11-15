"""Full AI pipeline orchestrator - the heart that connects every module.

One call chains the whole intelligence chain in the right order:

  1. SENSE      autodetect.py   live rainfall -> Bayesian evidence (optional)
  2. ASSESS     bayes.py        P(shelter accessible) -> risk-adjusted capacities
  3. OPTIMISE   compare.py      all 5 search algorithms on the RISK-ADJUSTED
                                scenario (optimisation under uncertainty)
  4. VALIDATE   logic.py        forward-chaining rule check of the mission plan
                                against the REAL capacities
  5. COORDINATE agents.py       contract-net negotiation for execution
  6. REPORT     gemini_service  grounded situation report for responders

The result: a deployable mission plan with an audit trail of every stage.
"""
from __future__ import annotations

import time

from . import agents as agents_mod
from . import autodetect as autodetect_mod
from . import bayes as bayes_mod
from . import compare as compare_mod
from . import logic as logic_mod
from .scenario import Scenario, Shelter


def run_pipeline(scenario: Scenario, evidence: dict | None = None,
                 live_weather: bool = False) -> dict:
    """Execute the full mission pipeline and return every stage's output."""
    t0 = time.perf_counter()
    stages = []

    def stage(name, fn):
        s = time.perf_counter()
        out = fn()
        stages.append({"stage": name, "ms": round((time.perf_counter() - s) * 1000, 1)})
        return out

    # 1. SENSE - optional live weather detection (falls back to given evidence)
    weather = None
    if live_weather:
        try:
            weather = stage("sense: live weather", lambda: autodetect_mod.detect_rain(scenario))
            evidence = weather["evidence"]
        except Exception as exc:
            stages.append({"stage": "sense: live weather (unavailable)",
                           "ms": 0.0, "note": str(exc)[:120]})
    evidence = evidence or {}

    # 2. ASSESS - probabilistic risk -> risk-adjusted capacities
    bayes_res = stage("assess: Bayesian risk",
                      lambda: bayes_mod.analyze(scenario, evidence))
    caps = bayes_res["risk_adjusted_capacities"]

    # 3. OPTIMISE - search under uncertainty (risk-adjusted scenario),
    #    plus a benchmark run on the original (fair-weather) scenario
    capped = Scenario(
        name=scenario.name + " (risk-adjusted)",
        disaster_type=scenario.disaster_type,
        max_travel_km=scenario.max_travel_km,
        zones=list(scenario.zones),
        shelters=[Shelter(s.id, s.name, caps.get(s.id, s.capacity),
                          s.medical, s.lat, s.lng)
                  for s in scenario.shelters],
    )
    benchmark = stage("optimise: benchmark (5 algorithms)",
                      lambda: compare_mod.run_all(scenario))
    mission = stage("optimise: risk-aware mission (5 algorithms)",
                    lambda: compare_mod.run_all(capped))

    mission_alloc = mission["winner_allocation"]
    mission_metrics = mission["rows"][0]

    # 4. VALIDATE - the mission plan must be legal against REAL capacities
    logic_res = stage("validate: logic engine",
                      lambda: logic_mod.analyze(scenario, mission_alloc))

    # 5. COORDINATE - contract-net execution on the real scenario
    agents_res = stage("coordinate: agent negotiation",
                       lambda: agents_mod.run_agents(scenario))

    deployable = (logic_res["verdict"] == "FEASIBLE"
                  and not mission_metrics["unallocated_people"])

    total_ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "stages": stages,
        "total_ms": total_ms,
        "evidence": evidence,
        "weather": {"locations": weather["locations"], "rain_level": weather["rain_level"],
                    "routes_blocked": weather["routes_blocked"]} if weather else None,
        "bayes": bayes_res,
        "benchmark": benchmark,
        "mission": {
            "scenario_name": capped.name,
            "winner": mission["winner"],
            "winner_label": mission["winner_label"],
            "winner_allocation": mission_alloc,
            "metrics": mission_metrics,
            "note": mission["note"],
            "deployable": deployable,
        },
        "logic": logic_res,
        "agents": agents_res,
    }
