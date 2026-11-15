"""ShelterOps AI - Flask backend.

Serves the frontend (frontend/) and exposes the JSON API the dashboard
calls. The frontend sends the current scenario with each request, so the
backend is stateless - every AI module is pure Python over the scenario.

Run:   python app.py   ->  http://127.0.0.1:5000
"""
from __future__ import annotations

import os

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from ai import agents, autodetect, bayes, compare, gemini_service, logic, pipeline
from ai.scenario import Scenario, default_scenario

load_dotenv()

app = Flask(__name__, static_folder="frontend", static_url_path="")
CORS(app)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def payload() -> dict:
    return request.get_json(silent=True) or {}


def scenario_from(data: dict) -> Scenario:
    return Scenario.from_dict(data["scenario"]) if data.get("scenario") \
        else default_scenario()


def ok(data):
    return jsonify({"ok": True, "data": data})


def fail(message, code=400):
    return jsonify({"ok": False, "error": str(message)}), code


@app.errorhandler(404)
def not_found(_e):
    return jsonify({"ok": False, "error": "not found"}), 404


@app.errorhandler(500)
def server_error(_e):
    return jsonify({"ok": False, "error": "internal server error"}), 500


# ----------------------------------------------------------------------
# frontend
# ----------------------------------------------------------------------
@app.get("/")
def index():
    return send_from_directory("frontend", "index.html")


@app.get("/<path:path>")
def static_files(path):
    if path.startswith("api/"):
        return fail("not found", 404)
    return send_from_directory("frontend", path)


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------
@app.get("/api/health")
def health():
    return ok({
        "app": "ShelterOps AI",
        "status": "running",
        "gemini_configured": gemini_service.is_configured(),
        "gemini_model": os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
    })


@app.get("/api/scenario")
def get_scenario():
    return ok(default_scenario().to_dict())


@app.post("/api/solve")
def solve():
    try:
        data = payload()
        scenario = scenario_from(data)
        algo = str(data.get("algo") or "greedy")
        return ok(compare.solve_one(algo, scenario))
    except Exception as exc:
        return fail(exc, 500)


@app.post("/api/compare")
def run_compare():
    try:
        scenario = scenario_from(payload())
        return ok(compare.run_all(scenario))
    except Exception as exc:
        return fail(exc, 500)


@app.post("/api/logic")
def run_logic():
    try:
        data = payload()
        scenario = scenario_from(data)
        allocation = data.get("allocation")
        if not allocation:
            allocation = compare.solve_one("greedy", scenario)["metrics"]["allocation"]
        return ok(logic.analyze(scenario, allocation))
    except Exception as exc:
        return fail(exc, 500)


@app.post("/api/bayes")
def run_bayes():
    try:
        data = payload()
        scenario = scenario_from(data)
        return ok(bayes.analyze(scenario, data.get("evidence") or {}))
    except Exception as exc:
        return fail(exc, 500)


@app.post("/api/agents")
def run_agents():
    try:
        scenario = scenario_from(payload())
        return ok(agents.run_agents(scenario))
    except Exception as exc:
        return fail(exc, 500)


@app.post("/api/pipeline")
def run_pipeline():
    """One call: sense -> assess -> optimise -> validate -> coordinate."""
    try:
        data = payload()
        scenario = scenario_from(data)
        ctx = data.get("context") or {}
        result = pipeline.run_pipeline(
            scenario,
            evidence=ctx.get("evidence"),
            live_weather=bool(data.get("live_weather")),
        )
        return ok(result)
    except Exception as exc:
        return fail(exc, 500)


@app.post("/api/autodetect")
def autodetect_region():
    """Build a whole scenario from real OpenStreetMap data around a point."""
    try:
        data = payload()
        lat = float(data.get("lat"))
        lng = float(data.get("lng"))
        radius = float(data.get("radius_km") or 25.0)
        scen = autodetect.detect_scenario(lat, lng, radius)
        if scen is None:
            return fail("OpenStreetMap has too few mapped settlements/shelters "
                        "there — try a bigger town, or enter data manually.", 404)
        return ok({"scenario": scen.to_dict(),
                   "source": "OpenStreetMap (ODbL) — auto-detected"})
    except Exception as exc:
        return fail(f"detection failed: {exc}", 502)


@app.post("/api/detect_risk")
def detect_risk():
    """Fetch live rainfall for every location and auto-set Bayesian evidence."""
    try:
        scenario = scenario_from(payload())
        det = autodetect.detect_rain(scenario)
        det["bayes"] = bayes.analyze(scenario, det["evidence"])
        return ok(det)
    except Exception as exc:
        return fail(f"live weather detection failed: {exc}", 502)


@app.post("/api/recommendations")
def recommendations():
    """Grounded AI situation report. Missing context pieces are computed
    server-side so a single click always produces a full report."""
    try:
        data = payload()
        scenario = scenario_from(data)
        ctx = data.get("context") or {}
        context = gemini_service.build_context(scenario, ctx)
        report = gemini_service.generate_report(context)
        report["context_used"] = list(context.keys())
        return ok(report)
    except Exception as exc:
        return fail(exc, 500)


@app.post("/api/chat")
def chat():
    try:
        data = payload()
        scenario = scenario_from(data)
        messages = data.get("messages") or []
        if not messages:
            return fail("messages[] required")
        ctx = data.get("context") or {}
        context = gemini_service.build_context(scenario, ctx, ctx.get("allocation"))
        return ok(gemini_service.chat(messages, context))
    except Exception as exc:
        return fail(exc, 500)


# ----------------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    print("=" * 62)
    print("  ShelterOps AI - Disaster Shelter Allocation prototype")
    print(f"  Open:  http://127.0.0.1:{port}")
    print(f"  Gemini API key configured: "
          f"{'YES' if gemini_service.is_configured() else 'NO (offline fallback active)'}")
    print("=" * 62)
    app.run(host="127.0.0.1", port=port, debug=True)
