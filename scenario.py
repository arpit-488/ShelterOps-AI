"""Scenario model: the world the AI reasons about.

A Scenario holds the affected *zones* (demand side: evacuees waiting to be
rescued) and the *shelters* (supply side: capacity and facilities), each at
real geographic coordinates (WGS84 lat/lng). Travel distance between a zone
and a shelter is the great-circle (haversine) distance in km.

The default scenario is a realistic Delhi Yamuna-flood situation: low-lying
riverside localities must be evacuated to real relief venues (stadiums,
hospital annex, Ramlila Maidan).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, asdict


@dataclass
class Zone:
    id: str
    name: str
    population: int      # number of evacuees to relocate
    priority: int        # 1 (low) .. 5 (critical: elderly / injured / children)
    medical_need: bool   # zone requires a shelter with medical facilities
    lat: float           # WGS84 latitude
    lng: float           # WGS84 longitude


@dataclass
class Shelter:
    id: str
    name: str
    capacity: int        # number of people it can host
    medical: bool        # has medical facilities
    lat: float
    lng: float


@dataclass
class Scenario:
    name: str
    disaster_type: str
    max_travel_km: float
    zones: list
    shelters: list

    # ---------------- lookups ----------------
    def zone(self, zid: str) -> Zone:
        for z in self.zones:
            if z.id == zid:
                return z
        raise KeyError(f"unknown zone: {zid}")

    def shelter(self, sid: str) -> Shelter:
        for s in self.shelters:
            if s.id == sid:
                return s
        raise KeyError(f"unknown shelter: {sid}")

    def distance(self, zid: str, sid: str) -> float:
        """Great-circle travel distance in km between a zone and a shelter."""
        z, s = self.zone(zid), self.shelter(sid)
        return haversine_km(z.lat, z.lng, s.lat, s.lng)

    # ---------------- aggregates ----------------
    @property
    def total_population(self) -> int:
        return sum(z.population for z in self.zones)

    @property
    def total_capacity(self) -> int:
        return sum(s.capacity for s in self.shelters)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "disaster_type": self.disaster_type,
            "max_travel_km": self.max_travel_km,
            "zones": [asdict(z) for z in self.zones],
            "shelters": [asdict(s) for s in self.shelters],
            "totals": {
                "population": self.total_population,
                "capacity": self.total_capacity,
                "medical_population": sum(z.population for z in self.zones if z.medical_need),
                "medical_capacity": sum(s.capacity for s in self.shelters if s.medical),
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Scenario":
        """Tolerant loader: clamps numbers, fills defaults, keeps ids unique."""
        zones, seen_z = [], set()
        for i, z in enumerate(data.get("zones", []) or []):
            zid = str(z.get("id") or f"Z{i + 1}")
            while zid in seen_z:
                zid += "x"
            seen_z.add(zid)
            zones.append(Zone(
                id=zid,
                name=str(z.get("name") or f"Zone {i + 1}"),
                population=max(0, int(z.get("population") or 0)),
                priority=max(1, min(5, int(z.get("priority") or 3))),
                medical_need=bool(z.get("medical_need")),
                lat=_clamp_lat(z.get("lat")),
                lng=_clamp_lng(z.get("lng")),
            ))
        shelters, seen_s = [], set()
        for j, s in enumerate(data.get("shelters", []) or []):
            sid = str(s.get("id") or f"S{j + 1}")
            while sid in seen_s:
                sid += "x"
            seen_s.add(sid)
            shelters.append(Shelter(
                id=sid,
                name=str(s.get("name") or f"Shelter {j + 1}"),
                capacity=max(0, int(s.get("capacity") or 0)),
                medical=bool(s.get("medical")),
                lat=_clamp_lat(s.get("lat")),
                lng=_clamp_lng(s.get("lng")),
            ))
        if not zones or not shelters:
            raise ValueError("scenario needs at least one zone and one shelter")
        return cls(
            name=str(data.get("name") or "Custom Scenario"),
            disaster_type=str(data.get("disaster_type") or "Flood"),
            max_travel_km=float(data.get("max_travel_km") or 9.0),
            zones=zones,
            shelters=shelters,
        )


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance between two WGS84 points, in km (2 decimals)."""
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 2)


def _clamp_lat(v) -> float:
    try:
        return max(6.0, min(37.0, float(v)))   # India bounds
    except (TypeError, ValueError):
        return 28.61                            # Delhi centre


def _clamp_lng(v) -> float:
    try:
        return max(68.0, min(98.0, float(v)))
    except (TypeError, ValueError):
        return 77.21


def default_scenario() -> Scenario:
    """Uttarakhand Flash Floods: a realistic Kedarnath-2013-style scenario.

    Evacuees from Himalayan valley towns (3,220 people) must reach real
    relief bases (4,200 places). Mountain geography makes it interesting:
    within the 60 km travel limit Srinagar can ONLY reach AIIMS Rishikesh
    and Uttarkashi ONLY the Dehradun base - so those beds must be reserved.

    A zero-violation plan exists, yet Greedy cannot find it: Kathgodam (P4)
    grabs the nearby Haldwani camp first, so when Nainital (P3) arrives
    there is no room left nearby and Greedy flies it ~140 km to Haridwar
    (way past the travel limit) - leaving Rudrapur camp completely EMPTY.
    Branch & Bound / the Genetic Algorithm reserve correctly: Nainital ->
    Haldwani, Kathgodam -> Rudrapur. Z1 (injured pilgrims) needs the only
    medical shelter: AIIMS Rishikesh.
    """
    zones = [
        Zone("Z1", "Srinagar (Pauri Garhwal)", 620, 5, True, 30.2226, 78.7809),
        Zone("Z2", "Kathgodam", 540, 4, False, 29.2620, 79.5250),
        Zone("Z3", "Uttarkashi", 410, 2, False, 30.7277, 78.4445),
        Zone("Z4", "Kotdwar", 760, 5, False, 29.7536, 78.5320),
        Zone("Z5", "Nainital", 560, 3, False, 29.3803, 79.4636),
        Zone("Z6", "Mussoorie", 330, 2, False, 30.4591, 78.0667),
    ]
    shelters = [
        Shelter("S1", "AIIMS Rishikesh", 900, True, 30.0760, 78.2690),
        Shelter("S2", "Dehradun - FRI Ground Camp", 800, False, 30.3399, 78.0369),
        Shelter("S3", "Haridwar - Roshanabad Base", 1400, False, 29.9500, 78.1570),
        Shelter("S4", "Haldwani Relief Camp", 560, False, 29.3924, 79.4543),
        Shelter("S5", "Rudrapur Relief Camp", 540, False, 28.9845, 79.4040),
    ]
    return Scenario(
        name="Uttarakhand Flash Floods",
        disaster_type="Flash Floods & Landslides (Himalaya)",
        max_travel_km=60.0,
        zones=zones,
        shelters=shelters,
    )
