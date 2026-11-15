"""Automatic detection pipeline - the system finds its own data.

Two live, key-free data sources power full automation:

1. OpenStreetMap (Overpass API)   -> real towns (with OSM population tags)
                                     and real shelter-candidate buildings
                                     (hospitals, schools, stadiums, ...)
                                     around any clicked map point.
2. Open-Meteo (weather API)       -> real current + 24h forecast rainfall for
                                     every zone/shelter, converted into
                                     Bayesian evidence automatically.

Robustness: mirror servers, a fast bounded query, and a guaranteed
estimated-scenario fallback so detection NEVER dead-ends - automation with
low human effort, always.
"""
from __future__ import annotations

import json
import math
import urllib.parse
import urllib.request

from .scenario import Scenario, Zone, Shelter, haversine_km

USER_AGENT = "ShelterOpsAI/1.0 (educational disaster-planning project)"
TIMEOUT_S = 35
OVERPASS_TIMEOUT = 20  # server-side query budget in the QL header

# capacity estimates per shelter-candidate type (places)
SHELTER_TYPES = {
    "hospital": (900, True),
    "stadium": (1400, False),
    "college": (800, False),
    "school": (700, False),
    "community_centre": (550, False),
    "sports_centre": (650, False),
    "place_of_worship": (450, False),
}

PRIORITY_LADDER = [5, 4, 5, 3, 2, 3, 4, 2]  # by population rank (deterministic)

OVERPASS_URLS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


# ----------------------------------------------------------------------
def _http_json(url: str, data: str | None = None) -> dict | list:
    req = urllib.request.Request(
        url,
        data=data.encode() if data else None,
        headers={"User-Agent": USER_AGENT,
                 "Content-Type": "application/x-www-form-urlencoded"
                 if data else "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        if resp.status != 200:
            raise RuntimeError(f"upstream returned HTTP {resp.status}")
        return json.loads(resp.read().decode("utf-8", "replace"))


def reverse_geocode(lat: float, lng: float) -> str | None:
    """Free Nominatim reverse geocode - used only for a human-readable name."""
    try:
        url = ("https://nominatim.openstreetmap.org/reverse?format=json&zoom=10"
               f"&lat={lat}&lon={lng}")
        data = _http_json(url)
        addr = data.get("address", {}) or {}
        return (addr.get("city") or addr.get("town") or addr.get("district")
                or addr.get("state_district") or data.get("name"))
    except Exception:
        return None


def _offset(lat: float, lng: float, d_km: float, bearing_deg: float) -> tuple:
    """Point d_km away from (lat, lng) at the given bearing."""
    d_lat = d_km / 111.32
    d_lng = d_km / (111.32 * max(0.2, math.cos(math.radians(lat))))
    return (round(lat + d_lat * math.cos(math.radians(bearing_deg)), 4),
            round(lng + d_lng * math.sin(math.radians(bearing_deg)), 4))


def _overpass(lat: float, lng: float, radius_m: int) -> dict | list:
    q = (
        f"[out:json][timeout:{OVERPASS_TIMEOUT}];"
        "("
        f'node["amenity"~"^(hospital|school|college|community_centre|place_of_worship)$"]'
        f'["name"](around:{radius_m},{lat},{lng});'
        f'way["amenity"~"^(hospital|school|college|community_centre|place_of_worship)$"]'
        f'["name"](around:{radius_m},{lat},{lng});'
        f'node["leisure"~"^(stadium|sports_centre)$"]["name"](around:{radius_m},{lat},{lng});'
        f'way["leisure"~"^(stadium|sports_centre)$"]["name"](around:{radius_m},{lat},{lng});'
        f'node["place"~"^(town|village|suburb|neighbourhood)$"](around:{radius_m},{lat},{lng});'
        ");"
        "out center 150;"
    )
    body = "data=" + urllib.parse.quote(q)
    last_err = None
    for url in OVERPASS_URLS:
        try:
            return _http_json(url, data=body)
        except Exception as exc:  # try next mirror
            last_err = exc
    raise RuntimeError(f"Overpass unavailable: {last_err}")


# ----------------------------------------------------------------------
# parsing helpers
# ----------------------------------------------------------------------
def _parse_shelters(elements, lat: float, lng: float) -> list:
    shelters, seen = [], set()
    for el in elements:
        tags = el.get("tags") or {}
        stype = tags.get("amenity") or tags.get("leisure")
        if stype not in SHELTER_TYPES:
            continue
        name = (tags.get("name") or "").strip()
        if not name:
            continue
        plat = el.get("lat") or (el.get("center") or {}).get("lat")
        plng = el.get("lon") or (el.get("center") or {}).get("lon")
        if plat is None or plng is None:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        cap, med = SHELTER_TYPES[stype]
        shelters.append({
            "name": name[:40], "capacity": cap, "medical": med,
            "lat": round(float(plat), 4), "lng": round(float(plng), 4),
            "d": haversine_km(lat, lng, float(plat), float(plng)),
        })
    shelters.sort(key=lambda s: s["d"])
    # drop physical duplicates (e.g. hospital + its blood bank mapped twice)
    deduped = []
    for s in shelters:
        if any(s["medical"] == d["medical"]
               and haversine_km(s["lat"], s["lng"], d["lat"], d["lng"]) < 0.35
               for d in deduped):
            continue
        deduped.append(s)
    return deduped[:5]


def _parse_places(elements, lat: float, lng: float) -> list:
    places = []
    for el in elements:
        tags = el.get("tags") or {}
        if tags.get("place") not in ("town", "village", "suburb", "neighbourhood"):
            continue
        zlat = el.get("lat") or (el.get("center") or {}).get("lat")
        zlng = el.get("lon") or (el.get("center") or {}).get("lon")
        if zlat is None or zlng is None:
            continue
        try:
            pop = int(tags.get("population") or 0)
        except (TypeError, ValueError):
            pop = 0
        places.append({
            "name": (tags.get("name") or "Settlement")[:30],
            "pop": pop, "lat": round(float(zlat), 4), "lng": round(float(zlng), 4),
            "d": haversine_km(lat, lng, float(zlat), float(zlng)),
        })
    places.sort(key=lambda p: (-p["pop"], p["d"]))
    return places[:6]


# ----------------------------------------------------------------------
# fallbacks (detection must never dead-end)
# ----------------------------------------------------------------------
_PAD_CAPS = [(900, True), (800, False), (1000, False), (600, False),
             (700, False), (500, False)]


def _pad_shelters(shelters: list, lat: float, lng: float, radius_km: float) -> list:
    """Top up real shelters with plausible relief bases so the optimiser
    always has a workable problem."""
    out = list(shelters)
    i = 0
    while len(out) < 4:
        slat, slng = _offset(lat, lng,
                             min(radius_km * 0.35, 6.0) * (0.5 + 0.12 * i),
                             35 + i * 80)
        cap, med = _PAD_CAPS[(len(out)) % len(_PAD_CAPS)]
        out.append({"name": f"Relief Base {len(out) + 1}", "capacity": cap,
                    "medical": med, "lat": slat, "lng": slng})
        i += 1
    return out[:5]


def _pad_places(places: list, lat: float, lng: float) -> list:
    out = list(places)
    while len(out) < 6:
        i = len(out)
        zlat, zlng = _offset(lat, lng, 2.5 + 1.6 * i, 30 + i * 55)
        out.append({"name": f"Sector {chr(65 + i)}", "pop": 0,
                    "lat": zlat, "lng": zlng})
    return out[:6]


def _synthetic_scenario(lat: float, lng: float, region: str,
                        radius_km: float) -> Scenario:
    """Full fallback when OpenStreetMap is unreachable: plausible scenario
    geometry around the clicked point (clearly labelled as estimated)."""
    ring = [(2.5, 30), (4.5, 120), (6.0, 210), (8.0, 300), (5.5, 60), (9.5, 150)]
    pops = [620, 540, 410, 760, 560, 330]
    prio = [5, 4, 5, 3, 3, 2]
    caps = [(900, True), (800, False), (1400, False), (560, False)]

    zones = [Zone(f"Z{i + 1}", f"Sector {chr(65 + i)}", pops[i], prio[i],
                  i == 0, *_offset(lat, lng, *ring[i]))
             for i in range(6)]
    shelters = [Shelter(f"S{i + 1}", n, c, m,
                        *_offset(lat, lng,
                                 min(radius_km * 0.4, 6) * (0.4 + 0.15 * i),
                                 40 + i * 75))
                for i, (n, (c, m)) in enumerate(zip(
                    ["Central Relief Camp", "District Hospital Shelter",
                     "Stadium Relief Base", "Community School Camp"], caps))]
    return Scenario(
        name=f"{region} (estimated — OSM unavailable)",
        disaster_type="Auto-detected Flood Risk (estimated)",
        max_travel_km=max(30.0, radius_km * 2),
        zones=zones,
        shelters=shelters,
    )


# ----------------------------------------------------------------------
def detect_scenario(lat: float, lng: float, radius_km: float = 15.0) -> Scenario:
    """Build a full scenario around a clicked point from real OSM data.
    Never fails: pads thin results and falls back to an estimated scenario
    when OpenStreetMap is unreachable."""
    region = reverse_geocode(lat, lng) or "Detected Region"
    try:
        data = _overpass(lat, lng, int(radius_km * 1000))
    except Exception:
        return _synthetic_scenario(lat, lng, region, radius_km)
    elements = data.get("elements", []) if isinstance(data, dict) else []

    shelters = _parse_shelters(elements, lat, lng)
    places = _parse_places(elements, lat, lng)

    if not shelters and not places:
        return _synthetic_scenario(lat, lng, region, radius_km)

    # thin OSM coverage -> blend real findings with padded relief bases /
    # sectors so the optimiser always has a workable problem
    shelters = _pad_shelters(shelters, lat, lng, radius_km)
    places = _pad_places(places, lat, lng)

    # affected share: ~10% of a settlement needs sheltering (capped for demo)
    zones = [{
        "name": p["name"], "lat": p["lat"], "lng": p["lng"],
        "population": max(80, min(1200, int(p["pop"] * 0.10))) if p["pop"]
                      else 300,
    } for p in places]
    zones.sort(key=lambda z: -z["population"])

    # deterministic priorities by population rank + medical need near a hospital
    hosp = [s for s in shelters if s["medical"]]
    med_zone = None
    if hosp:
        near = [(haversine_km(z["lat"], z["lng"], h["lat"], h["lng"]), z)
                for z in zones for h in hosp]
        if near:
            med_zone = min(near, key=lambda t: t[0])[1]
            med_zone = med_zone if min(t[0] for t in near) <= 6.0 else None
    for i, z in enumerate(zones):
        z["priority"] = PRIORITY_LADDER[i % len(PRIORITY_LADDER)]
        z["medical_need"] = (med_zone is z)

    return Scenario(
        name=f"{region} (auto-detected)",
        disaster_type="Auto-detected Flood Risk (OSM + live weather)",
        max_travel_km=60.0,
        zones=[Zone("Z%d" % (i + 1), z["name"], z["population"], z["priority"],
                    z["medical_need"], z["lat"], z["lng"])
               for i, z in enumerate(zones)],
        shelters=[Shelter("S%d" % (i + 1), s["name"], s["capacity"], s["medical"],
                          s["lat"], s["lng"])
                  for i, s in enumerate(shelters)],
    )


# ----------------------------------------------------------------------
# Live disaster-risk detection from real weather data
# ----------------------------------------------------------------------
def detect_rain(scenario: Scenario) -> dict:
    """Fetch real current + 24h forecast precipitation for every location and
    convert it into Bayesian evidence automatically."""
    pts = ([("zone", z.id, z.name, z.lat, z.lng) for z in scenario.zones]
           + [("shelter", s.id, s.name, s.lat, s.lng) for s in scenario.shelters])
    lats = ",".join(str(p[3]) for p in pts)
    lngs = ",".join(str(p[4]) for p in pts)
    url = ("https://api.open-meteo.com/v1/forecast"
           f"?latitude={lats}&longitude={lngs}"
           "&current=precipitation&hourly=precipitation&forecast_days=2")

    raw = _http_json(url)
    rows = raw if isinstance(raw, list) else [raw]

    locations, rain_level, blocked = [], "low", []
    for (kind, ident, name, lat, lng), row in zip(pts, rows):
        cur = float(((row.get("current") or {}).get("precipitation")) or 0.0)
        hourly = (row.get("hourly") or {}).get("precipitation") or []
        try:
            max24 = max(float(x or 0.0) for x in hourly[:24])
        except ValueError:
            max24 = 0.0
        if cur >= 5.0 or max24 >= 25.0:
            lvl = "high"
        elif cur >= 1.0 or max24 >= 8.0:
            lvl = "moderate"
        else:
            lvl = "low"
        locations.append({"kind": kind, "id": ident, "name": name,
                          "current_mm": round(cur, 1),
                          "max24_mm": round(max24, 1), "level": lvl})
        if lvl == "high":
            rain_level = "high"
        elif lvl == "moderate" and rain_level != "high":
            rain_level = "moderate"
        if kind == "shelter" and lvl == "high":
            blocked.append(ident)

    evidence = {"rain": rain_level}
    for sid in blocked:
        evidence[f"route_blocked_{sid}"] = True

    return {
        "locations": locations,
        "rain_level": rain_level,
        "routes_blocked": blocked,
        "evidence": evidence,
        "source": "Open-Meteo live weather",
    }
