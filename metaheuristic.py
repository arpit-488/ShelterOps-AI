"""Modern optimisation metaheuristics compared against classical search.

- simulated_annealing(): stochastic hill climbing that accepts some worse
  moves early (temperature) to escape local optima.
- genetic_algorithm(): population-based search with tournament selection,
  uniform crossover, mutation and elitism.

Both optimise the same penalised score as ai.search so every approach in
the comparison tab is measured on one scale.
"""
from __future__ import annotations

import math
import random

from .search import penalized


def simulated_annealing(scenario, t0: float = 1500.0, cooling: float = 0.997,
                        iters: int = 4000, seed: int = 7) -> dict:
    rng = random.Random(seed)
    sids = [s.id for s in scenario.shelters]
    zids = [z.id for z in scenario.zones]

    current = {z: rng.choice(sids) for z in zids}
    cur = penalized(scenario, current)
    best, best_score = dict(current), cur
    curve, accepted = [round(cur, 1)], 0
    t = t0

    for it in range(1, iters + 1):
        cand = dict(current)
        zid = rng.choice(zids)
        others = [s for s in sids if s != cand[zid]] or sids
        cand[zid] = rng.choice(others)
        c = penalized(scenario, cand)
        delta = c - cur
        # Metropolis acceptance: better always, worse with probability e^(-d/T)
        if delta < 0 or rng.random() < math.exp(-max(delta, 1e-9) / max(t, 1e-9)):
            current, cur = cand, c
            accepted += 1
            if cur < best_score:
                best, best_score = dict(current), cur
        t *= cooling
        if it % 50 == 0:
            curve.append(round(best_score, 1))

    return {
        "allocation": best,
        "curve": curve,
        "iterations": iters,
        "acceptance_rate": round(accepted / max(1, iters), 3),
        "final_temperature": round(t, 2),
        "best_score": round(best_score, 1),
    }


def genetic_algorithm(scenario, pop_size: int = 50, generations: int = 100,
                      crossover_rate: float = 0.9, mutation_rate: float = 0.2,
                      elite: int = 4, seed: int = 11) -> dict:
    """Individual = list of shelter ids, one gene per zone (zone-index order)."""
    rng = random.Random(seed)
    sids = [s.id for s in scenario.shelters]
    zones = list(scenario.zones)
    zids = [z.id for z in zones]
    n = len(zids)

    def to_alloc(ind):
        return dict(zip(zids, ind))

    memo = {}

    def fitness(ind):
        key = tuple(ind)
        if key not in memo:
            memo[key] = penalized(scenario, to_alloc(ind))
        return memo[key]

    population = [[rng.choice(sids) for _ in range(n)] for _ in range(pop_size)]
    history = []

    for _ in range(generations):
        scored = sorted(population, key=fitness)
        history.append(round(fitness(scored[0]), 1))
        new_pop = [list(ind) for ind in scored[:elite]]  # elitism
        while len(new_pop) < pop_size:
            # tournament selection (k=3)
            def tournament():
                cands = rng.sample(population, 3)
                return min(cands, key=fitness)
            p1, p2 = tournament(), tournament()
            if rng.random() < crossover_rate:      # uniform crossover
                child = [p1[i] if rng.random() < 0.5 else p2[i] for i in range(n)]
            else:
                child = list(p1)
            for i in range(n):                     # bit-flip style mutation
                if rng.random() < mutation_rate:
                    child[i] = rng.choice(sids)
            new_pop.append(child)
        population = new_pop

    best = min(population, key=fitness)
    return {
        "allocation": to_alloc(best),
        "curve": history,
        "generations": generations,
        "population_size": pop_size,
        "best_score": round(fitness(best), 1),
    }
