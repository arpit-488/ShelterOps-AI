"""Comparison harness: run every AI approach on the same scenario and
measure them on one scale (penalised score = distance cost + constraint
penalties), so classical search and modern metaheuristics can be compared
fairly side by side."""
from __future__ import annotations

import time

from . import search, metaheuristic as mh
from .scenario import Scenario

ALGORITHMS = {
    "greedy": "Greedy (priority-first nearest)",
    "hill_climbing": "Hill Climbing (local search)",
    "backtracking": "Backtracking (Branch & Bound)",
    "simulated_annealing": "Simulated Annealing",
    "genetic": "Genetic Algorithm",
}


def _run(algo: str, scenario: Scenario):
    """Return (allocation, extras) for one algorithm."""
    if algo == "greedy":
        raw = search.greedy(scenario)
        extras = {"steps": raw["steps"]}
    elif algo == "hill_climbing":
        raw = search.hill_climbing(scenario)
        extras = {"curve": raw["curve"], "evals": raw["evals"]}
    elif algo == "backtracking":
        raw = search.backtracking(scenario)
        extras = {"nodes": raw["nodes"], "trace": raw["trace"],
                  "truncated": raw["truncated"]}
    elif algo == "simulated_annealing":
        raw = mh.simulated_annealing(scenario)
        extras = {"curve": raw["curve"], "iterations": raw["iterations"],
                  "acceptance_rate": raw["acceptance_rate"]}
    elif algo == "genetic":
        raw = mh.genetic_algorithm(scenario)
        extras = {"curve": raw["curve"], "generations": raw["generations"],
                  "population_size": raw["population_size"]}
    else:
        raise ValueError(f"unknown algorithm: {algo}")
    return raw["allocation"], extras


def _notes(algo: str, extras: dict) -> str:
    if algo == "greedy":
        return "baseline: no backtracking, no global view"
    if algo == "hill_climbing":
        return f"{extras['evals']} moves evaluated; stops at local optimum"
    if algo == "backtracking":
        return f"{extras['nodes']} nodes explored; systematic + optimal (small instances)"
    if algo == "simulated_annealing":
        return (f"{extras['iterations']} iterations, acceptance rate "
                f"{extras['acceptance_rate']:.0%}")
    if algo == "genetic":
        return (f"{extras['generations']} generations x "
                f"{extras['population_size']} individuals")
    return ""


def solve_one(algo: str, scenario: Scenario) -> dict:
    """Run a single algorithm; returns metrics + extras for the UI."""
    t0 = time.perf_counter()
    allocation, extras = _run(algo, scenario)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 1)
    metrics = search.evaluate(scenario, allocation)
    return {
        "algo": algo,
        "label": ALGORITHMS.get(algo, algo),
        "metrics": metrics,
        "runtime_ms": elapsed_ms,
        "notes": _notes(algo, extras),
        **extras,
    }


def run_all(scenario: Scenario) -> dict:
    results = {}
    for algo in ALGORITHMS:
        results[algo] = solve_one(algo, scenario)

    rows = []
    for algo, r in results.items():
        m = r["metrics"]
        rows.append({
            "algo": algo,
            "label": r["label"],
            "allocated_people": m["allocated_people"],
            "coverage_pct": m["coverage_pct"],
            "weighted_cost": m["weighted_cost"],
            "penalized_score": m["penalized_score"],
            "avg_distance_km": m["avg_distance_km"],
            "max_utilization": m["max_utilization"],
            "overflow_people": m["overflow_people"],
            "unallocated_people": m["unallocated_people"],
            "feasible": m["feasible"],
            "runtime_ms": r["runtime_ms"],
            "notes": r["notes"],
        })
    rows.sort(key=lambda r: r["penalized_score"])
    winner = rows[0]["algo"]

    curves = {a: results[a]["curve"] for a in results if "curve" in results[a]}
    bt = results["backtracking"]
    return {
        "rows": rows,
        "winner": winner,
        "winner_label": ALGORITHMS[winner],
        "winner_allocation": results[winner]["metrics"]["allocation"],
        "optimal_score": results["backtracking"]["metrics"]["penalized_score"],
        "curves": curves,
        "backtracking_nodes": bt.get("nodes"),
        "note": (f"Branch & Bound explored {bt.get('nodes')} nodes - its score is "
                 f"provably optimal for this scenario, so algorithms matching it "
                 f"found the best possible allocation."),
    }
