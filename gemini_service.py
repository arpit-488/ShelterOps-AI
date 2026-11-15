"""Gemini API integration - grounded AI reports and analyst chat.

Grounding rule: the model only receives numbers that the deterministic AI
modules actually computed. `build_context()` assembles a compact, verified
context (scenario, algorithm comparison, Bayesian risk, agent coordination,
logical validation of the current plan) plus a list of pre-computed
"insights". If no API key is configured or the call fails, smart offline
fallbacks answer from the same data, so the project always works offline.
"""
from __future__ import annotations

import json
import os

from . import agents as agents_mod
from . import bayes as bayes_mod
from . import compare as compare_mod
from . import logic as logic_mod

try:
    from google import genai as _genai
    _SDK_AVAILABLE = True
except Exception:  # pragma: no cover - SDK simply not installed
    _genai = None
    _SDK_AVAILABLE = False


def _client():
    """Return (client_or_None, reason_if_unavailable)."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not _SDK_AVAILABLE:
        return None, "google-genai SDK not installed (pip install google-genai)"
    if not key or key == "your_key_here":
        return None, "GEMINI_API_KEY not set in .env"
    try:
        return _genai.Client(api_key=key), None
    except Exception as exc:  # bad key format etc.
        return None, str(exc)


def _model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip() or "gemini-2.5-flash"


def is_configured() -> bool:
    client, _ = _client()
    return client is not None


# ======================================================================
# Grounded context assembly (shared by report + chat + offline analyst)
# ======================================================================
def build_context(scenario, ctx: dict | None = None,
                  allocation: dict | None = None) -> dict:
    """Compact, verified context for the LLM and the offline analyst.

    Missing pieces are computed server-side so a single call always has the
    full picture. Heavy fields (curves, traces, long logs) are dropped.
    """
    ctx = ctx or {}

    comparison = ctx.get("comparison")
    if not comparison or not comparison.get("rows"):
        comparison = compare_mod.run_all(scenario)
    comp_c = _compact_comparison(comparison)

    bayes = ctx.get("bayes")
    if not bayes or not bayes.get("shelters"):
        bayes = bayes_mod.analyze(scenario, {})
    bayes_c = _compact_bayes(bayes)

    agents = ctx.get("agents")
    if not agents or "metrics" not in agents:
        agents = agents_mod.run_agents(scenario)
    agents_c = _compact_agents(agents)

    alloc = allocation or comparison.get("winner_allocation") or {}
    logic_res = logic_mod.analyze(scenario, alloc)

    from .search import evaluate  # local import to avoid module cycles
    plan_metrics = evaluate(scenario, alloc) if alloc else None

    zone_names = {z.id: z.name for z in scenario.zones}
    insights = _compute_insights(scenario, comp_c, bayes_c, agents_c,
                                 logic_res, plan_metrics, zone_names)

    return {
        "scenario": {
            "name": scenario.name,
            "disaster_type": scenario.disaster_type,
            "max_travel_km": scenario.max_travel_km,
            "totals": scenario.to_dict()["totals"],
            "zones": [{"id": z.id, "name": z.name, "population": z.population,
                       "priority": z.priority, "medical_need": z.medical_need}
                      for z in scenario.zones],
            "shelters": [{"id": s.id, "name": s.name, "capacity": s.capacity,
                          "medical": s.medical} for s in scenario.shelters],
        },
        "comparison": comp_c,
        "bayes": bayes_c,
        "agents": agents_c,
        "logic": _compact_logic(logic_res),
        "plan": _compact_plan(alloc, plan_metrics, zone_names),
        "insights": insights,
    }


def _compact_comparison(comparison: dict) -> dict:
    rows = [{
        "algo": r["algo"],
        "label": r["label"],
        "score": r["penalized_score"],
        "coverage_pct": r["coverage_pct"],
        "avg_distance_km": r["avg_distance_km"],
        "overflow_people": r["overflow_people"],
        "unallocated_people": r["unallocated_people"],
        "feasible": r["feasible"],
        "runtime_ms": r["runtime_ms"],
        "notes": r["notes"],
    } for r in comparison.get("rows", [])]
    return {
        "rows": rows,
        "winner": comparison.get("winner"),
        "winner_label": comparison.get("winner_label"),
        "optimal_score": comparison.get("optimal_score"),
    }


def _compact_bayes(bayes: dict) -> dict:
    return {
        "shelters": [{
            "shelter": s["shelter"], "name": s["name"],
            "capacity": s["capacity"],
            "p_accessible": s["p_accessible"],
            "risk_adjusted_capacity": s["risk_adjusted_capacity"],
        } for s in bayes.get("shelters", [])],
        "demand_surge_probability": bayes.get("demand_surge_probability"),
        "risky_shelters": bayes.get("risky_shelters") or {},
        "risky_people": bayes.get("risky_people", 0),
    }


def _compact_agents(agents: dict) -> dict:
    return {
        "messages": agents.get("messages"),
        "escalations": agents.get("escalations"),
        "coverage_pct": agents.get("metrics", {}).get("coverage_pct"),
        "completion_time_min": agents.get("completion_time_min"),
    }


def _compact_logic(logic_res: dict) -> dict:
    return {
        "verdict": logic_res.get("verdict"),
        "violations": [v["message"] for v in logic_res.get("violations", [])][:6],
        "unallocated_zones": logic_res.get("unallocated_zones", []),
        "urgent_unallocated_zones": logic_res.get("urgent_unallocated_zones", []),
    }


def _compact_plan(allocation: dict, metrics: dict | None, zone_names) -> dict | None:
    if not metrics:
        return None
    return {
        "allocation": {z: s for z, s in allocation.items()},
        "shelter_loads": metrics.get("shelter_loads"),
        "utilization": metrics.get("utilization"),
        "coverage_pct": metrics.get("coverage_pct"),
        "avg_distance_km": metrics.get("avg_distance_km"),
        "overflow_people": metrics.get("overflow_people"),
        "distance_violations_people": metrics.get("distance_violations_people"),
        "medical_violations_people": metrics.get("medical_violations_people"),
        "feasible": metrics.get("feasible"),
    }


def _compute_insights(scenario, comp, bayes, agents, logic_res, plan_metrics,
                      zone_names) -> list:
    """Deterministic findings the analyst can trust and reuse verbatim."""
    out = []
    rows = comp.get("rows") or []
    winner = next((r for r in rows if r["algo"] == comp.get("winner")), None)
    greedy = next((r for r in rows if r["algo"] == "greedy"), None)

    if winner:
        out.append(f"Best approach: {winner['label']} (score {winner['score']:,}, "
                   f"coverage {winner['coverage_pct']}%).")
    if greedy and comp.get("optimal_score") and greedy["score"] > comp["optimal_score"]:
        gap = greedy["score"] - comp["optimal_score"]
        out.append(f"Greedy costs {gap:,.0f} points more than the optimal plan "
                   f"(+{gap / max(1, comp['optimal_score']):.0%}).")

    # forced reservations from the Bayesian network
    for s in bayes.get("shelters", []):
        if s["p_accessible"] < 0.5:
            out.append(f"{s['name']} is unreliable: P(accessible) = "
                       f"{s['p_accessible']:.0%}, plan only "
                       f"{s['risk_adjusted_capacity']}/{s['capacity']} places.")
    surge = bayes.get("demand_surge_probability")
    if surge is not None:
        level = "High" if surge >= 0.5 else "Moderate" if surge >= 0.25 else "Low"
        out.append(f"{level} demand-surge risk ({surge:.0%}).")

    if agents.get("escalations"):
        out.append(f"{agents['escalations']} zone(s) escalated by the agent "
                   f"simulation - external relief camps requested.")
    if logic_res.get("verdict"):
        out.append(f"Logical validation of the current plan: {logic_res['verdict']}.")
        for v in logic_res.get("violations", [])[:3]:
            out.append(f"Violation: {v}")

    if plan_metrics:
        crowded = {sid: u for sid, u in (plan_metrics.get("utilization") or {}).items()
                   if u > 0.9}
        for sid, u in sorted(crowded.items(), key=lambda kv: -kv[1]):
            out.append(f"Shelter {sid} is at {u:.0%} utilization "
                       f"({'OVER' if u > 1 else 'near limit'}).")
    return out[:12]


# ======================================================================
# Gemini report
# ======================================================================
REPORT_PROMPT = """You are "ShelterOps Analyst", an AI emergency-management advisor inside a disaster shelter-allocation system.

STRICT GROUNDING RULES:
- Use ONLY the numbers and facts in the CONTEXT block. Never invent or estimate figures.
- The "insights" list contains pre-verified findings computed by the system - weave them in.
- If something is missing from the data, say so explicitly instead of guessing.

Write a situation report in Markdown with exactly these sections:
## 1. Situation Overview
## 2. Allocation Analysis (compare the algorithms; explain WHY the winner wins)
## 3. Risk Assessment (Bayesian probabilities, surge, unreliable routes)
## 4. Recommendations (numbered, concrete: what, why with numbers, risk if ignored)
## 5. Operational Next Steps (next 6 hours / 24 hours / 48 hours)

Keep it under 500 words, professional tone, no preamble.

CONTEXT:
```json
{context_json}
```"""


def generate_report(context: dict) -> dict:
    """Generate the grounded situation report via Gemini (or fallback)."""
    client, err = _client()
    if client is not None:
        try:
            prompt = REPORT_PROMPT.format(
                context_json=json.dumps(context, indent=2, default=str)[:14000])
            resp = client.models.generate_content(
                model=_model(), contents=prompt,
                config={"temperature": 0.4},
            )
            text = (getattr(resp, "text", "") or "").strip()
            if text:
                return {"text": text, "source": "gemini", "model": _model()}
            err = "Gemini returned an empty response"
        except Exception as exc:
            err = str(exc)
    return {"text": fallback_report(context), "source": "fallback",
            "model": None, "error": err}


# ======================================================================
# Gemini chat
# ======================================================================
CHAT_SYSTEM = """You are "ShelterOps Analyst", the embedded AI advisor of a disaster
shelter-allocation system (scenario: {scenario_name}).

ANSWER RULES:
1. GROUNDING - every number must come from the CONTEXT JSON. Never invent or
   extrapolate. If the data cannot answer, say exactly what is missing and
   which app tab would provide it (Scenario / Algorithms / Reasoning / Agents).
2. BREVITY - under 150 words. Markdown bullets, **bold the key numbers**.
3. CAUSE AND EFFECT - prefer "Kathgodam (P4) took the Haldwani camp first ->
   Nainital (P3) had no room within 60 km -> flown 140 km to Haridwar ->
   +560 distance violations (+33,600 penalty)".
4. ADVICE - for action questions give concrete steps that reference real
   entities in the data (which shelter, how many people, which algorithm to
   re-run, which evidence to update).
5. The CONTEXT "insights" list holds pre-verified findings - trust and reuse them.

CONTEXT:
```json
{context_json}
```

CONVERSATION SO FAR:
{transcript}

ASSISTANT:"""


def chat(messages: list, context: dict) -> dict:
    client, err = _client()
    if client is not None:
        try:
            convo = "\n".join(
                f"{'USER' if (m.get('role') != 'assistant') else 'ASSISTANT'}: "
                f"{m.get('content', '')}" for m in (messages or [])[-8:]
            )
            prompt = (CHAT_SYSTEM
                      .replace("{scenario_name}",
                               (context.get("scenario") or {}).get("name", "?"))
                      .replace("{context_json}",
                               json.dumps(context, default=str)[:9000])
                      .replace("{transcript}", convo or "(new conversation)")
                      + "\n\nUSER: "
                      + str((messages or [{}])[-1].get("content", "")))
            resp = client.models.generate_content(
                model=_model(), contents=prompt,
                config={"temperature": 0.3},
            )
            text = (getattr(resp, "text", "") or "").strip()
            if text:
                return {"text": text, "source": "gemini", "model": _model()}
            err = "Gemini returned an empty response"
        except Exception as exc:
            err = str(exc)
    return {"text": fallback_chat(messages, context), "source": "fallback",
            "model": None, "error": err}


# ----------------------------------------------------------------------
# Offline fallbacks (project works fully without any API key)
# ----------------------------------------------------------------------
import re


def _has(q: str, *words) -> bool:
    """Word-start matching: 'cover' matches 'coverage', but 'hi' does not
    match 'which'."""
    return any(re.search(rf"\b{re.escape(w)}", q) for w in words)
def fallback_report(context: dict) -> str:
    sc = context.get("scenario") or {}
    totals = sc.get("totals") or {}
    comp = context.get("comparison") or {}
    rows = comp.get("rows") or []
    bay = context.get("bayes") or {}
    ag = context.get("agents") or {}
    insights = context.get("insights") or []

    lines = [
        "# ShelterOps Situation Report (offline template)",
        "> Generated without Gemini - add GEMINI_API_KEY to .env for the full AI report.",
        "",
        "## 1. Situation Overview",
        f"- Scenario: **{sc.get('name', 'unknown')}** ({sc.get('disaster_type', '')})",
        f"- Evacuees: **{totals.get('population', '?')}** across {len(sc.get('zones', []))} zones",
        f"- Shelter capacity: **{totals.get('capacity', '?')}** places "
        f"({len(sc.get('shelters', []))} shelters, medical capacity "
        f"{totals.get('medical_capacity', '?')})",
        "",
        "## 2. Allocation Analysis",
    ]
    if rows:
        for r in rows[:5]:
            lines.append(f"- **{r['label']}**: score {r['score']:,}, "
                         f"coverage {r['coverage_pct']}%, avg distance "
                         f"{r['avg_distance_km']} km, {r['runtime_ms']} ms")
        lines.append(f"- Winner: **{comp.get('winner_label', '?')}** "
                     f"(optimal reference score from Branch & Bound: "
                     f"{comp.get('optimal_score', '?'):,})")
    else:
        lines.append("- No algorithm run recorded yet.")

    lines += ["", "## 3. Risk Assessment"]
    for s in (bay.get("shelters") or [])[:6]:
        lines.append(f"- {s['name']}: P(accessible) = {s['p_accessible']:.0%} "
                     f"-> plan for {s['risk_adjusted_capacity']}/{s['capacity']} places")
    if bay.get("demand_surge_probability") is not None:
        lines.append(f"- Demand surge probability: {bay['demand_surge_probability']:.0%}")

    lines += ["", "## 4. Recommendations"]
    recs = list(insights[:4]) or ["Plan is feasible - maintain it and monitor routes."]
    lines += [f"{i}. {r}" for i, r in enumerate(recs, 1)]

    lines += ["", "## 5. Operational Next Steps",
              "- Next 6 h: confirm the winning plan with the logic validator; brief transport teams.",
              "- Next 24 h: re-run Bayesian inference as road/damage reports arrive; re-allocate.",
              "- Next 48 h: audit capacity vs demand surge; open external camps if coverage < 100%."]
    return "\n".join(lines)


# ---------------- offline rule-based analyst ----------------
def _off(note: str) -> str:
    return (f"*(Offline analyst — add GEMINI_API_KEY to .env for the full AI "
            f"version.)*\n\n{note}")


def fallback_chat(messages: list, context: dict) -> str:
    q = ""
    for m in reversed(messages or []):
        if (m.get("role") or "user") == "user":
            q = str(m.get("content", "")).lower()
            break

    sc = context.get("scenario") or {}
    comp = context.get("comparison") or {}
    rows = comp.get("rows") or []
    plan = context.get("plan") or {}
    bay = context.get("bayes") or {}
    agents = context.get("agents") or {}
    logic_res = context.get("logic") or {}
    insights = context.get("insights") or []

    def has(*words):
        return _has(q, *words)

    winner = next((r for r in rows if r["algo"] == comp.get("winner")), None)
    greedy = next((r for r in rows if r["algo"] == "greedy"), None)

    if re.search(r"^\s*(hi|hey|hello|help)\b", q) or "what can you" in q:
        return _off(
            "I'm the ShelterOps analyst. I can answer from the data this app "
            "computed, e.g.:\n"
            "- **Which shelter is most at risk and why?**\n"
            "- **Why did Greedy lose to Branch & Bound?**\n"
            "- **Which shelters are near capacity?**\n"
            "- **What should we do in the next 24 hours?**\n\n"
            f"Current scenario: **{sc.get('name', '?')}** "
            f"({sc.get('totals', {}).get('population', '?')} evacuees, "
            f"{sc.get('totals', {}).get('capacity', '?')} places).")

    if has("risk", "risky", "danger", "safe", "access", "rain", "block"):
        sh = sorted(bay.get("shelters") or [], key=lambda s: s["p_accessible"])
        if not sh:
            return _off("Run the Bayesian inference on the **Reasoning** tab first.")
        lines = ["**Shelter reliability (P accessible, risk-adjusted capacity):**"]
        for s in sh[:3]:
            lines.append(f"- **{s['name']}**: {s['p_accessible']:.0%} accessible "
                         f"-> plan {s['risk_adjusted_capacity']}/{s['capacity']} places")
        if len({s["p_accessible"] for s in sh}) == 1:
            lines.append("- *No evidence set yet — set rain / route-blocked evidence "
                         "on the Reasoning tab for sharper answers.*")
        surge = bay.get("demand_surge_probability")
        if surge is not None:
            lines.append(f"- Demand-surge probability: **{surge:.0%}**")
        if bay.get("risky_people"):
            lines.append(f"- **{bay['risky_people']} people** are routed through "
                         f"shelters whose route is probably blocked.")
        return _off("\n".join(lines))

    if has("greedy", "lose", "lost", "fail", "worse", "why") or \
            has("algorithm", "compare", "best", "winner", "bound", "genetic"):
        if not rows:
            return _off("Run **Run all & compare** on the Algorithms tab first.")
        lines = [f"**Winner: {comp.get('winner_label')}** "
                 f"(score {comp.get('optimal_score'):,})."]
        if greedy and comp.get("optimal_score") and greedy["score"] > comp["optimal_score"]:
            gap = greedy["score"] - comp["optimal_score"]
            lines.append(f"- Greedy scored **{greedy['score']:,.0f}** — "
                         f"**{gap:,.0f} points worse (+{gap / max(1, comp['optimal_score']):.0%})**.")
        for r in rows:
            if r["overflow_people"]:
                lines.append(f"- {r['label']}: **{r['overflow_people']} people over capacity**.")
            if r["unallocated_people"]:
                lines.append(f"- {r['label']}: **{r['unallocated_people']} people unplaced**.")
        lines.append("- Greedy never revises earlier choices; Branch & Bound reserves "
                     "scarce shelters (e.g. the only one within 60 km) for the zones "
                     "that truly need them.")
        return _off("\n".join(lines))

    if has("overcrowd", "crowd", "full", "capacity", "load", "util"):
        loads = plan.get("shelter_loads") or {}
        util = plan.get("utilization") or {}
        names = {s["id"]: s["name"] for s in sc.get("shelters", [])}
        if not util:
            return _off("Run an algorithm first so I have a plan to inspect.")
        lines = ["**Shelter utilization in the current plan:**"]
        for sid, u in sorted(util.items(), key=lambda kv: -kv[1]):
            lines.append(f"- **{names.get(sid, sid)}**: {loads.get(sid, 0):,} / "
                         f"{dict((s['id'], s['capacity']) for s in sc.get('shelters', [])).get(sid, '?'):,}"
                         f" (**{u:.0%}**{' — OVER' if u > 1 else ''})")
        return _off("\n".join(lines))

    if has("cover", "unalloc", "place", "left", "how many", "people", "population",
           "total", "summary", "status"):
        t = sc.get("totals") or {}
        lines = [f"**{sc.get('name', '?')}** ({sc.get('disaster_type', '')}):",
                 f"- Evacuees: **{t.get('population', '?')}** · capacity: "
                 f"**{t.get('capacity', '?')}** · medical capacity: "
                 f"**{t.get('medical_capacity', '?')}**"]
        if winner:
            lines.append(f"- Best plan covers **{winner['coverage_pct']}%** "
                         f"(avg distance **{winner['avg_distance_km']} km**).")
        if logic_res.get("verdict"):
            lines.append(f"- Logic validation: **{logic_res['verdict']}**")
        if agents:
            lines.append(f"- Agent simulation: **{agents.get('coverage_pct')}%** "
                         f"placed, {agents.get('escalations')} escalation(s), "
                         f"{agents.get('completion_time_min')} min.")
        return _off("\n".join(lines))

    if has("do", "next", "action", "recommend", "should", "advice", "step"):
        lines = ["**Recommended actions (from computed data):**"]
        acts = list(insights[:4])
        if not acts:
            acts = ["Plan is feasible — maintain it and monitor routes."]
        lines += [f"{i}. {a}" for i, a in enumerate(acts, 1)]
        lines.append("- Re-run **Run all & compare** after any capacity change.")
        return _off("\n".join(lines))

    if insights:
        return _off("**Key findings from the computed data:**\n" +
                    "\n".join(f"- {i}" for i in insights[:6]) +
                    "\n\nAsk me about risk, algorithms, capacity, or next actions.")
    return _off("I don't have computed data yet — run an algorithm on the "
                "**Algorithms** tab, then ask me again.")
