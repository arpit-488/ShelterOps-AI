/* ============================================================
   ShelterOps AI — dashboard logic (vanilla JS, no build step)
   ============================================================ */
"use strict";

/* ---------------- helpers ---------------- */
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  scenario: null,
  lastSolve: null,      // {algo, label, metrics, ...}
  lastCompare: null,
  lastBayes: null,
  lastAgents: null,
  traces: {},           // algo -> {label, lines:[{cls,text}]}
  charts: {},
  chat: [],
};

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const fmt = (n) => Number(n ?? 0).toLocaleString("en-US");

let toastTimer = null;
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("error", isError);
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3400);
}

async function api(path, body = null) {
  const opts = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new Error("Backend not reachable — is app.py running?");
  }
  const json = await res.json().catch(() => ({}));
  if (!json.ok) throw new Error(json.error || `HTTP ${res.status}`);
  return json.data;
}

/* ---------------- tabs ---------------- */
$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest(".tab");
  if (!btn) return;
  $$(".tab").forEach((t) => t.classList.toggle("active", t === btn));
  $$(".tab-panel").forEach((p) => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
  if (btn.dataset.tab === "dashboard" && leafletMap) {
    setTimeout(() => leafletMap.invalidateSize(), 80);
  }
});

/* ============================================================
   DASHBOARD : KPIs, map, plan table
   ============================================================ */
function renderChips() {
  const sc = state.scenario;
  if (!sc) return;
  const pop = sc.zones.reduce((a, z) => a + (Number(z.population) || 0), 0);
  const cap = sc.shelters.reduce((a, s) => a + (Number(s.capacity) || 0), 0);
  $("#chipScenario").innerHTML = `🗺️ <b>${esc(sc.name)}</b>`;
  $("#chipPop").innerHTML = `👥 <b>${fmt(pop)}</b> evacuees`;
  $("#chipCap").innerHTML = `🏠 <b>${fmt(cap)}</b> places`;
  const cov = state.lastSolve ? state.lastSolve.metrics.coverage_pct : null;
  $("#chipCoverage").innerHTML = cov !== null ? `✅ <b>${cov}%</b> covered` : "✅ —";
}

function renderKPIs() {
  const sc = state.scenario, s = state.lastSolve;
  if (!sc) return;
  $("#kpiEvacuees").textContent = fmt(sc.totals.population);
  $("#kpiCapacity").textContent = fmt(sc.totals.capacity);
  if (!s) return;
  const m = s.metrics;
  $("#kpiCoverage").textContent = `${m.coverage_pct}%`;
  $("#kpiCoverage").className = `kpi-value ${m.coverage_pct >= 99.9 ? "good" : m.coverage_pct >= 90 ? "" : "bad"}`;
  $("#kpiDistance").textContent = `${m.avg_distance_km} km`;
  $("#kpiUtil").textContent = `${Math.round(m.max_utilization * 100)}%`;
  const ok = m.feasible;
  $("#kpiStatus").textContent = ok ? "FEASIBLE" : "ISSUES";
  $("#kpiStatus").className = `kpi-value ${ok ? "good" : "bad"}`;
  $("#dashAlgoLabel").textContent = `plan: ${s.label} · score ${fmt(m.penalized_score)}`;
}

/* ----- real map (Leaflet + OpenStreetMap tiles, free / no API key) ----- */
let leafletMap = null;
let leafletLayers = null;
let leafletFitScenario = null;
let detectMode = false;
let detecting = false;

function zoneColor(priority) {
  return { 5: "#ef4444", 4: "#f97316", 3: "#f59e0b" }[priority] || "#64748b";
}

function ensureLeaflet() {
  if (typeof L === "undefined") return false; // CDN blocked / offline
  if (!leafletMap) {
    // Google-Maps-style layer switcher: streets / satellite / terrain (all key-free)
    const osm = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    });
    const sat = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 19,
      attribution: "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics",
    });
    const terrain = L.tileLayer("https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png", {
      maxZoom: 17,
      attribution: 'Map data: &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, SRTM | &copy; <a href="https://opentopomap.org">OpenTopoMap</a> (CC-BY-SA)',
    });
    leafletMap = L.map("leafletMap", {
      scrollWheelZoom: false,   // enabled while hovering (see below)
      touchZoom: true,          // pinch-zoom on touchscreens
      doubleClickZoom: true,    // double-click / double-tap
      boxZoom: true,            // Shift + drag = zoom to selected area
      dragging: true,           // pan with mouse / finger
      keyboard: true,           // arrows pan, +/- zoom (after clicking map)
      zoomSnap: 0.25,           // smooth fractional steps for scroll/pinch
      zoomDelta: 1,             // full level per double-click / key press
      layers: [osm],
    });
    L.control.layers(
      { "🗺️ Streets (OSM)": osm, "🛰️ Satellite (Esri)": sat, "⛰️ Terrain (Topo)": terrain },
      null, { position: "topright" }
    ).addTo(leafletMap);
    leafletLayers = L.layerGroup().addTo(leafletMap);

    // Gesture rule: wheel / trackpad-pinch zoom ONLY while the cursor is
    // over the map, so scrolling the rest of the page stays normal.
    leafletMap.on("mouseenter", () => leafletMap.scrollWheelZoom.enable());
    leafletMap.on("mouseleave", () => leafletMap.scrollWheelZoom.disable());

    // ---- Google-Maps-style gestures ----
    // Double RIGHT-click zooms out (centred on the click point), like Google Maps.
    let lastRightClick = 0;
    leafletMap.on("contextmenu", (e) => {
      if (e.originalEvent) e.originalEvent.preventDefault();
      const now = Date.now();
      if (now - lastRightClick < 450) {
        leafletMap.setZoomAround(e.containerPoint, leafletMap.getZoom() - 1);
        lastRightClick = 0;
      } else {
        lastRightClick = now;
      }
    });

    // Two-finger double-tap zooms out on touchscreens.
    const box = leafletMap.getContainer();
    let twoFingerStart = 0, lastTwoFingerTap = 0;
    box.addEventListener("touchstart", (ev) => {
      if (ev.touches.length === 2) twoFingerStart = Date.now();
    }, { passive: true });
    box.addEventListener("touchend", (ev) => {
      if (ev.touches.length === 0 && twoFingerStart
          && Date.now() - twoFingerStart < 250) {
        const now = Date.now();
        if (now - lastTwoFingerTap < 500) {
          leafletMap.zoomOut(1);
          lastTwoFingerTap = 0;
        } else {
          lastTwoFingerTap = now;
        }
        twoFingerStart = 0;
      }
    }, { passive: true });

    // One-click auto-detection: click the map (or any marker), the backend
    // builds the whole scenario from real OpenStreetMap data around it.
    leafletMap.on("click", (e) => {
      if (detectMode) autoDetectAt(e.latlng);
    });
    leafletMap.on("popupopen", (e) => {
      if (detectMode && e.popup && e.popup.getLatLng()) {
        autoDetectAt(e.popup.getLatLng());
      }
    });

    // exposed for debugging / tests
    window._leafletMap = leafletMap;
  }
  return true;
}

function renderMap() {
  const sc = state.scenario;
  if (!sc || !ensureLeaflet()) return;
  leafletLayers.clearLayers();
  const alloc = state.lastSolve ? state.lastSolve.metrics.allocation : {};
  const loads = state.lastSolve ? state.lastSolve.metrics.shelter_loads : {};
  const bounds = [];

  // allocation flow lines (zone -> assigned shelter) — dark, bold, readable
  for (const z of sc.zones) {
    const sid = alloc[z.id];
    if (!sid) continue;
    const s = sc.shelters.find((x) => x.id === sid);
    if (!s) continue;
    L.polyline([[z.lat, z.lng], [s.lat, s.lng]], {
      color: "#c2410c",
      weight: 2.5 + z.population / 150,
      opacity: 0.9,
      dashArray: "8 6",
    }).bindTooltip(`${z.id} → ${sid} · ${dist(sc, z, s)} km`)
      .addTo(leafletLayers);
  }

  // shelters (real relief venues) with primary-coverage area shading
  for (const s of sc.shelters) {
    const load = loads[s.id] || 0;
    const col = s.medical ? "#0ea5e9" : "#059669";
    L.circle([s.lat, s.lng], {
      radius: 15000,
      color: col, weight: 1.5, opacity: 0.35,
      fillColor: col, fillOpacity: 0.07,
      interactive: false,
    }).addTo(leafletLayers);
    const icon = L.divIcon({
      className: "shelter-icon",
      html: `<div class="box ${s.medical ? "med" : "std"}">${s.medical ? "✚" : "🏠"}</div>`,
      iconSize: [26, 26],
      iconAnchor: [13, 13],
    });
    L.marker([s.lat, s.lng], { icon, zIndexOffset: 500 })
      .bindTooltip(s.id, { permanent: true, direction: "top",
                           offset: [0, -15], className: "pin-label" })
      .bindPopup(
        `<b>${esc(s.id)} · ${esc(s.name)}</b><br>` +
        `Capacity <b>${fmt(s.capacity)}</b> places` +
        (load ? `<br>Load <b>${fmt(load)}</b> (${Math.round((100 * load) / Math.max(1, s.capacity))}%)` : "") +
        (s.medical ? "<br>⚕️ Medical unit" : "")
      )
      .addTo(leafletLayers);
    bounds.push([s.lat, s.lng]);
  }

  // zones (affected localities, sized by population, coloured by priority)
  for (const z of sc.zones) {
    const sid = alloc[z.id];
    const placed = Boolean(sid);
    const r = 8 + Math.sqrt(z.population) / 2.2;
    L.circleMarker([z.lat, z.lng], {
      radius: r,
      color: "#ffffff",
      weight: 3,
      fillColor: zoneColor(z.priority),
      fillOpacity: 0.95,
    })
      .bindTooltip(`${z.id} · P${z.priority}`, {
        permanent: true, direction: "top",
        offset: [0, -r - 3], className: "pin-label",
      })
      .bindPopup(
        `<b>${esc(z.id)} · ${esc(z.name)}</b><br>` +
        `${fmt(z.population)} evacuees · priority ${z.priority}/5` +
        (z.medical_need ? "<br>⚕️ needs a medical shelter" : "") +
        (placed
          ? `<br>Assigned: <b>${esc(sid)}</b> (${dist(sc, z, sc.shelters.find((x) => x.id === sid))} km)`
          : "<br><b>⚠️ UNPLACED</b>")
      )
      .addTo(leafletLayers);
    if (!placed) {
      L.circleMarker([z.lat, z.lng], {
        radius: r + 6, color: "#dc2626", weight: 2, fill: false,
        className: "zone-pulse", interactive: false,
      }).addTo(leafletLayers);
    }
    bounds.push([z.lat, z.lng]);
  }

  // fit the view once per scenario edit (not on every re-plan)
  if (bounds.length && leafletFitScenario !== sc) {
    leafletMap.fitBounds(bounds, { padding: [42, 42], maxZoom: 14 });
    leafletFitScenario = sc;
  }
}

function renderPlanTable() {
  const sc = state.scenario, s = state.lastSolve;
  if (!sc || !s) return;
  const alloc = s.metrics.allocation;
  const loads = s.metrics.shelter_loads;
  const rows = sc.zones.map((z) => {
    const sid = alloc[z.id];
    const sh = sid ? sc.shelters.find((x) => x.id === sid) : null;
    const d = sh ? dist(sc, z, sh) : null;
    return `<tr>
      <td><b>${esc(z.id)}</b> ${esc(z.name)}</td>
      <td><span class="tag ${z.priority >= 4 ? "no" : "warn"}">P${z.priority}${z.medical_need ? " · med" : ""}</span></td>
      <td>${fmt(z.population)}</td>
      <td>${sh ? `<b>${esc(sh.id)}</b> ${esc(sh.name)}` : `<span class="tag no">UNPLACED</span>`}</td>
      <td>${d !== null ? d + " km" : "—"}</td>
      <td>${sh ? fmt(loads[sh.id] || 0) + " / " + fmt(sh.capacity) : "—"}</td>
    </tr>`;
  });
  $("#planBody").innerHTML = rows.join("");
}

/* client-side haversine mirror of ai/scenario.py (display only) */
function dist(sc, z, s) {
  const R = 6371.0088;
  const rad = (d) => (d * Math.PI) / 180;
  const p1 = rad(z.lat), p2 = rad(s.lat);
  const dp = rad(s.lat - z.lat), dl = rad(s.lng - z.lng);
  const a = Math.sin(dp / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) ** 2;
  return Math.round(2 * R * Math.asin(Math.sqrt(a)) * 100) / 100;
}

/* ----- live shelter status (green / yellow / red) ----- */
function renderShelterStatus() {
  const sc = state.scenario;
  if (!sc) return;
  const loads = state.lastSolve ? state.lastSolve.metrics.shelter_loads : null;
  const risk = {};
  if (state.lastBayes) {
    for (const s of state.lastBayes.shelters || []) risk[s.shelter] = s.p_accessible;
  }
  $("#statusGrid").innerHTML = sc.shelters.map((s) => {
    const load = loads ? (loads[s.id] || 0) : 0;
    const util = load / Math.max(1, s.capacity);
    const p = risk[s.id];
    const atRisk = typeof p === "number" && p < 0.5;
    const cls = (util >= 1 || atRisk) ? "bad" : util >= 0.8 ? "warn" : "ok";
    const word = util >= 1 ? (s.medical ? "FULL ⚕️" : "FULL")
      : atRisk ? "AT RISK"
      : util >= 0.8 ? "FILLING" : "AVAILABLE";
    return `<div class="status-card ${cls}">
      <span class="status-title">${esc(s.id)} · ${esc(s.name)}</span>
      <span class="status-load">${fmt(load)} / ${fmt(s.capacity)} places (${Math.round(util * 100)}%)</span>
      <div class="status-foot">
        <span class="status-word">${word}</span>
        ${s.medical ? '<span class="tag ok">med</span>' : ""}
        ${atRisk ? `<span class="tag no">P ${Math.round(p * 100)}%</span>` : ""}
      </div>
    </div>`;
  }).join("");
}

function renderDashboard() {
  renderChips();
  renderKPIs();
  renderShelterStatus();
  renderMap();
  renderPlanTable();
}

/* ============================================================
   FULL AI MISSION — one click runs the entire pipeline
   ============================================================ */
function renderMissionBanner(res) {
  const banner = $("#missionBanner");
  banner.classList.remove("hidden", "bad");
  const m = res.mission;
  const stages = res.stages.map((s) => `${esc(s.stage)} ${s.ms}ms`).join(" · ");
  if (m.deployable) {
    banner.innerHTML = `🚀 <b>MISSION PLAN READY</b> — ${esc(m.winner_label)} on `
      + `risk-adjusted capacities · validated <b>FEASIBLE</b> · coverage `
      + `<b>${m.metrics.coverage_pct}%</b> · risk-adjusted score ${fmt(m.metrics.penalized_score)}.`
      + `<span class="muted">Pipeline: ${stages} · total ${res.total_ms} ms — human approval is the only step left.</span>`;
  } else {
    banner.classList.add("bad");
    const un = m.metrics.unallocated_people || 0;
    banner.innerHTML = `⚠️ <b>MISSION PLAN NEEDS APPROVAL</b> — ${esc(m.winner_label)} on `
      + `risk-adjusted capacities · ${esc(res.logic.verdict)} · `
      + `${un ? `<b>${fmt(un)} people cannot be placed safely</b> — request external relief camps.` : "review violations below."}`
      + `<span class="muted">Pipeline: ${stages} · total ${res.total_ms} ms</span>`;
  }
}

$("#btnMission").addEventListener("click", async () => {
  const btn = $("#btnMission");
  btn.disabled = true;
  btn.textContent = "🚀 Running mission…";
  try {
    const res = await api("/api/pipeline", {
      scenario: state.scenario,
      live_weather: true,
    });
    state.lastBayes = res.bayes;
    state.lastCompare = res.benchmark;
    state.lastAgents = res.agents;
    const row = res.mission.metrics;
    const loads = winnerRowShelterLoads(res.mission.winner_allocation);
    const util = winnerRowUtilization(res.mission.winner_allocation);
    // KPIs must reflect REAL capacities: the mission optimised against the
    // conservative risk-adjusted caps, but deployment legality is real-caps.
    const realOverflow = state.scenario.shelters.reduce(
      (a, s) => a + Math.max(0, (loads[s.id] || 0) - s.capacity), 0);
    const allPlaced = state.scenario.zones.every(
      (z) => res.mission.winner_allocation[z.id]);
    state.lastSolve = {
      algo: res.mission.winner,
      label: res.mission.winner_label,
      metrics: {
        ...row,
        allocation: res.mission.winner_allocation,
        shelter_loads: loads,
        utilization: util,
        max_utilization: Math.max(0, ...Object.values(util)),
        overflow_people: realOverflow,
        feasible: realOverflow === 0 && allPlaced,
      },
    };
    renderMissionBanner(res);
    renderDashboard();
    renderBayesControls();
    if ($("#evRain") && res.evidence && res.evidence.rain) {
      $("#evRain").value = res.evidence.rain;
    }
    toast(`🚀 Mission complete in ${res.total_ms} ms — plan ${res.mission.deployable ? "ready to deploy" : "needs approval"}.`);
  } catch (err) {
    toast(err.message, true);
  }
  btn.disabled = false;
  btn.textContent = "🚀 Run full AI mission";
});

/* ============================================================
   AUTO-DETECTION (low human effort: click -> detect -> plan)
   ============================================================ */
function setDetectMode(on) {
  detectMode = on;
  const btn = $("#btnDetectMode");
  btn.classList.toggle("detect-active", on);
  btn.textContent = on ? "🎯 Now click the map…" : "🛰️ Auto-detect from map";
  if (leafletMap) {
    leafletMap.getContainer().style.cursor = on ? "crosshair" : "";
  }
}

async function autoDetectAt(latlng) {
  if (detecting) return;
  detecting = true;
  const btn = $("#btnDetectMode");
  let elapsed = 0;
  const timer = setInterval(() => {
    elapsed += 1;
    btn.textContent = `🛰️ Detecting… ${elapsed}s (scanning OSM mirrors)`;
  }, 1000);
  toast("🛰️ Scanning OpenStreetMap around your click — watch the button for progress…");
  try {
    const res = await api("/api/autodetect", {
      lat: latlng.lat, lng: latlng.lng, radius_km: 15,
    });
    state.scenario = res.scenario;
    leafletFitScenario = null;
    state.lastCompare = null;
    state.lastBayes = null;
    state.lastAgents = null;
    state.lastSolve = await api("/api/solve", { algo: "greedy", scenario: state.scenario });
    renderScenarioEditor();
    renderBayesControls();
    renderDashboard();
    setDetectMode(false);
    const sc = res.scenario;
    toast(`✅ Detected ${sc.zones.length} settlements & ${sc.shelters.length} shelters `
          + `in ${elapsed}s — scenario, plan & map rebuilt automatically!`);
  } catch (err) {
    toast(err.message, true);
    setDetectMode(false);
  }
  clearInterval(timer);
  detecting = false;
}

$("#btnDetectMode").addEventListener("click", () => setDetectMode(!detectMode));

$("#btnDetectRain").addEventListener("click", async () => {
  const btn = $("#btnDetectRain");
  btn.disabled = true;
  btn.textContent = "⏳ Fetching live weather…";
  try {
    const res = await api("/api/detect_risk", { scenario: state.scenario });
    // reflect the auto-detected evidence in the UI controls
    if ($("#evRain")) $("#evRain").value = res.evidence.rain || "unknown";
    $$("[data-ev]").forEach((sel) => {
      const v = res.evidence[sel.dataset.ev];
      sel.value = v === true ? "yes" : v === false ? "no" : "unknown";
    });
    state.lastBayes = res.bayes;
    renderBayes(res.bayes);
    const worst = res.locations.reduce(
      (a, b) => (b.max24_mm > (a ? a.max24_mm : -1) ? b : a), null);
    toast(`🌧️ Live weather: worst 24 h ${worst ? worst.max24_mm : 0} mm`
          + `${worst ? " near " + worst.name : ""} → rain level `
          + `${res.rain_level.toUpperCase()}`
          + `${res.routes_blocked.length ? " · " + res.routes_blocked.length + " route(s) auto-flagged" : ""}`
          + ` — Bayesian plan updated automatically!`);
  } catch (err) {
    toast(err.message, true);
  }
  btn.disabled = false;
  btn.textContent = "🛰️ Detect live rain";
});

/* ============================================================
   SCENARIO EDITOR
   ============================================================ */
function renderScenarioEditor() {
  const sc = state.scenario;
  if (!sc) return;
  $("#scName").value = sc.name;
  $("#scType").value = sc.disaster_type;
  $("#scMaxTravel").value = sc.max_travel_km;

  $("#zonesBody").innerHTML = sc.zones.map((z) => rowZone(z)).join("");
  $("#sheltersBody").innerHTML = sc.shelters.map((s) => rowShelter(s)).join("");
}

function rowZone(z) {
  return `<tr>
    <td><b>${esc(z.id)}</b></td>
    <td><input data-kind="zone" data-id="${esc(z.id)}" data-field="name" value="${esc(z.name)}" size="16"/></td>
    <td><input data-kind="zone" data-id="${esc(z.id)}" data-field="population" type="number" min="0" step="10" value="${z.population}" size="6"/></td>
    <td><input data-kind="zone" data-id="${esc(z.id)}" data-field="priority" type="number" min="1" max="5" value="${z.priority}" size="3"/></td>
    <td><input data-kind="zone" data-id="${esc(z.id)}" data-field="medical_need" type="checkbox" ${z.medical_need ? "checked" : ""}/></td>
    <td><input data-kind="zone" data-id="${esc(z.id)}" data-field="lat" type="number" step="0.0001" min="6" max="37" value="${z.lat}" size="8"/></td>
    <td><input data-kind="zone" data-id="${esc(z.id)}" data-field="lng" type="number" step="0.0001" min="68" max="98" value="${z.lng}" size="8"/></td>
    <td><button class="btn small" data-del="zone" data-id="${esc(z.id)}">✕</button></td>
  </tr>`;
}

function rowShelter(s) {
  return `<tr>
    <td><b>${esc(s.id)}</b></td>
    <td><input data-kind="shelter" data-id="${esc(s.id)}" data-field="name" value="${esc(s.name)}" size="16"/></td>
    <td><input data-kind="shelter" data-id="${esc(s.id)}" data-field="capacity" type="number" min="0" step="50" value="${s.capacity}" size="6"/></td>
    <td><input data-kind="shelter" data-id="${esc(s.id)}" data-field="medical" type="checkbox" ${s.medical ? "checked" : ""}/></td>
    <td><input data-kind="shelter" data-id="${esc(s.id)}" data-field="lat" type="number" step="0.0001" min="6" max="37" value="${s.lat}" size="8"/></td>
    <td><input data-kind="shelter" data-id="${esc(s.id)}" data-field="lng" type="number" step="0.0001" min="68" max="98" value="${s.lng}" size="8"/></td>
    <td><button class="btn small" data-del="shelter" data-id="${esc(s.id)}">✕</button></td>
  </tr>`;
}

document.addEventListener("input", (e) => {
  const el = e.target;
  const kind = el.dataset.kind;
  if (!kind) return;
  const arr = kind === "zone" ? state.scenario.zones : state.scenario.shelters;
  const obj = arr.find((o) => o.id === el.dataset.id);
  if (!obj) return;
  const f = el.dataset.field;
  if (el.type === "checkbox") obj[f] = el.checked;
  else if (el.type === "number") obj[f] = Number(el.value);
  else obj[f] = el.value;
});

document.addEventListener("click", async (e) => {
  const del = e.target.closest("[data-del]");
  if (del) {
    const kind = del.dataset.del;
    const key = kind === "zone" ? "zones" : "shelters";
    state.scenario[key] = state.scenario[key].filter((o) => o.id !== del.dataset.id);
    renderScenarioEditor();
  }
});

function nextId(prefix, arr) {
  let max = 0;
  for (const o of arr) {
    const n = parseInt(String(o.id).replace(/\D/g, ""), 10);
    if (!isNaN(n) && n > max) max = n;
  }
  return `${prefix}${max + 1}`;
}

$("#btnAddZone").addEventListener("click", () => {
  state.scenario.zones.push({
    id: nextId("Z", state.scenario.zones), name: "New Zone",
    population: 300, priority: 3, medical_need: false, lat: 28.61, lng: 77.21,
  });
  renderScenarioEditor();
});

$("#btnAddShelter").addEventListener("click", () => {
  state.scenario.shelters.push({
    id: nextId("S", state.scenario.shelters), name: "New Shelter",
    capacity: 500, medical: false, lat: 28.61, lng: 77.21,
  });
  renderScenarioEditor();
});

["scName", "scType", "scMaxTravel"].forEach((id) => {
  $("#" + id).addEventListener("input", (e) => {
    const v = e.target.value;
    if (id === "scName") state.scenario.name = v;
    if (id === "scType") state.scenario.disaster_type = v;
    if (id === "scMaxTravel") state.scenario.max_travel_km = Number(v) || 15;
  });
});

$("#btnSaveScenario").addEventListener("click", async () => {
  try {
    state.lastSolve = await api("/api/solve", { algo: "greedy", scenario: state.scenario });
    state.lastCompare = null; // stale
    leafletFitScenario = null; // re-fit map to (possibly edited) locations
    renderDashboard();
    toast("Scenario saved — dashboard re-planned with Greedy.");
  } catch (err) { toast(err.message, true); }
});

$("#btnResetScenario").addEventListener("click", async () => {
  try {
    state.scenario = await api("/api/scenario");
    state.lastSolve = await api("/api/solve", { algo: "greedy", scenario: state.scenario });
    renderScenarioEditor();
    renderDashboard();
    toast("Reset to default Uttarakhand Flash Floods scenario.");
  } catch (err) { toast(err.message, true); }
});

/* ============================================================
   ALGORITHMS TAB
   ============================================================ */
$$(".algo-buttons .btn[data-algo]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const res = await api("/api/solve", { algo: btn.dataset.algo, scenario: state.scenario });
      state.lastSolve = res;
      if (res.algo === "greedy" && res.steps) {
        state.traces.greedy = {
          label: "Greedy — assignment steps",
          lines: res.steps.map((st) => ({
            cls: "t-assign",
            text: `${st.zone} → ${st.shelter ?? "UNPLACED"}  (${st.distance ?? "-"} km)  ${st.note}`,
          })),
        };
      }
      if (res.algo === "backtracking" && res.trace) {
        state.traces.backtracking = {
          label: `Backtracking — ${fmt(res.nodes)} nodes explored`,
          lines: res.trace.map((ev) => ({
            cls: ev.event === "prune" ? "t-prune" : ev.event === "complete" ? "t-complete" : "t-assign",
            text: `d${ev.depth} [${ev.event}] ${ev.note}${ev.score !== undefined ? "  score " + fmt(ev.score) : ""}`,
          })),
        };
      }
      refreshTracePicker();
      renderDashboard();
      toast(`${res.label} done — score ${fmt(res.metrics.penalized_score)}, coverage ${res.metrics.coverage_pct}%.`);
    } catch (err) { toast(err.message, true); }
    btn.disabled = false;
  });
});

$("#btnRunAll").addEventListener("click", async () => {
  const btn = $("#btnRunAll");
  btn.disabled = true;
  btn.textContent = "⏳ Running all 5 approaches…";
  try {
    const res = await api("/api/compare", { scenario: state.scenario });
    state.lastCompare = res;
    const winnerRow = res.rows.find((r) => r.algo === res.winner);
    state.lastSolve = {
      algo: res.winner, label: res.winner_label,
      metrics: {
        ...winnerRow,
        allocation: res.winner_allocation,
        shelter_loads: winnerRowShelterLoads(res.winner_allocation),
        utilization: winnerRowUtilization(res.winner_allocation),
      },
    };
    renderCompare(res);
    renderDashboard();
    toast(`Comparison done — winner: ${res.winner_label}.`);
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
  btn.textContent = "⚡ Run all & compare";
});

function winnerRowShelterLoads(allocation) {
  const loads = {};
  for (const s of state.scenario.shelters) loads[s.id] = 0;
  for (const z of state.scenario.zones) {
    const sid = allocation[z.id];
    if (sid && sid in loads) loads[sid] += z.population;
  }
  return loads;
}
function winnerRowUtilization(allocation) {
  const loads = winnerRowShelterLoads(allocation);
  const u = {};
  for (const s of state.scenario.shelters) u[s.id] = loads[s.id] / Math.max(1, s.capacity);
  return u;
}

function renderCompare(res) {
  $("#winnerBanner").classList.remove("hidden");
  $("#winnerBanner").innerHTML =
    `🏆 <b>${esc(res.winner_label)}</b> wins with score ${fmt(res.rows[0].penalized_score)}. ` +
    `Optimal reference (Branch &amp; Bound): ${fmt(res.optimal_score)} · ${esc(res.note)}`;

  $("#algoBody").innerHTML = res.rows.map((r, i) => `<tr class="${i === 0 ? "winner-row" : ""}">
    <td>${i + 1}</td>
    <td><b>${esc(r.label)}</b><div class="muted small">${esc(r.notes)}</div></td>
    <td>${fmt(r.penalized_score)}</td>
    <td>${r.coverage_pct}%</td>
    <td>${fmt(r.weighted_cost)}</td>
    <td>${r.avg_distance_km} km</td>
    <td>${r.overflow_people ? `<span class="tag no">+${fmt(r.overflow_people)}</span>` : "—"}</td>
    <td>${r.unallocated_people ? `<span class="tag no">${fmt(r.unallocated_people)}</span>` : "—"}</td>
    <td>${r.feasible ? '<span class="tag ok">yes</span>' : '<span class="tag no">no</span>'}</td>
    <td>${r.runtime_ms} ms</td>
  </tr>`).join("");

  drawChart("score", $("#chartScore"), {
    type: "bar",
    data: {
      labels: res.rows.map((r) => r.label.split(" (")[0]),
      datasets: [{
        label: "Penalized score (lower = better)",
        data: res.rows.map((r) => r.penalized_score),
        backgroundColor: res.rows.map((r, i) => (i === 0 ? "#10b981" : "#f97316")),
        borderRadius: 6,
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true } },
    },
  });

  drawChart("coverage", $("#chartCoverage"), {
    type: "bar",
    data: {
      labels: res.rows.map((r) => r.label.split(" (")[0]),
      datasets: [{
        label: "Coverage %",
        data: res.rows.map((r) => r.coverage_pct),
        backgroundColor: "#0ea5e9",
        borderRadius: 6,
      }],
    },
    options: {
      plugins: { legend: { display: false } },
      scales: { y: { beginAtZero: true, suggestedMax: 100 } },
    },
  });

  const curves = Object.entries(res.curves || {});
  if (curves.length) {
    const maxLen = Math.max(...curves.map(([, c]) => c.length));
    drawChart("conv", $("#chartConvergence"), {
      type: "line",
      data: {
        labels: Array.from({ length: maxLen }, (_, i) => i),
        datasets: curves.map(([algo, c], i) => ({
          label: algo.replace("_", " "),
          data: c,
          borderColor: ["#f97316", "#0284c7", "#10b981", "#d97706"][i % 4],
          backgroundColor: "transparent",
          pointRadius: 0,
          tension: 0.25,
        })),
      },
      options: {
        scales: { y: { beginAtZero: false } },
        plugins: { title: { display: true, text: "Best score per iteration / generation" } },
      },
    });
  }
}

function drawChart(key, canvas, cfg) {
  if (typeof Chart === "undefined") return;
  if (state.charts[key]) state.charts[key].destroy();
  state.charts[key] = new Chart(canvas, cfg);
}

/* ----- trace viewer ----- */
function refreshTracePicker() {
  const pick = $("#tracePicker");
  const entries = Object.entries(state.traces);
  pick.innerHTML = entries.length
    ? entries.map(([algo, t]) => `<option value="${algo}">${esc(t.label)}</option>`).join("")
    : '<option value="">— pick a run —</option>';
  if (entries.length) showTrace(entries[0][0]);
}
function showTrace(algo) {
  const t = state.traces[algo];
  if (!t) return;
  $("#traceView").innerHTML = t.lines
    .map((l) => `<span class="${l.cls}">${esc(l.text)}</span>`)
    .join("\n");
}
$("#tracePicker").addEventListener("change", (e) => showTrace(e.target.value));

/* ============================================================
   REASONING TAB
   ============================================================ */
$("#btnRunLogic").addEventListener("click", async () => {
  try {
    const body = { scenario: state.scenario };
    if (state.lastSolve) body.allocation = state.lastSolve.metrics.allocation;
    const res = await api("/api/logic", body);
    renderLogic(res);
  } catch (err) { toast(err.message, true); }
});

function renderLogic(res) {
  const v = $("#logicVerdict");
  const bad = res.violations.length > 0 || res.unallocated_zones.length > 0;
  v.textContent = res.verdict;
  v.className = `verdict ${bad ? "bad" : "ok"}`;

  $("#logicViolations").innerHTML = (res.violations.length
    ? res.violations.map((x) => `<div class="violation">⚠️ ${esc(x.message)}</div>`).join("")
    : '<span class="muted">No constraint violations 🎉</span>')
    + (res.unallocated_zones.length
      ? `<div class="violation">🚨 Unallocated zones: ${res.unallocated_zones.join(", ")}`
        + (res.urgent_unallocated_zones.length
          ? ` (urgent: ${res.urgent_unallocated_zones.join(", ")})` : "")
        + "</div>"
      : "");

  $("#logicFired").innerHTML = res.fired_rules.length
    ? res.fired_rules.map((r) => `<div class="rule-card">
        <b>${esc(r.rule)}</b> <span class="cnt">fired → ${r.count} fact(s)</span>
        <code>${esc(r.form)}</code>
        <div class="muted small">${r.derived.map(esc).join(" · ")}</div>
      </div>`).join("")
    : '<span class="muted">No rules fired (no allocation to validate — run an algorithm first).</span>';

  $("#factCount").textContent = res.total_facts;
  $("#logicFacts").innerHTML = res.facts.slice(0, 250).map((f) => `<span class="fact">${esc(f)}</span>`).join("");
}

/* ----- Bayesian ----- */
function renderBayesControls() {
  const sc = state.scenario;
  if (!sc) return;
  let html = `<label>Rain severity
    <select id="evRain"><option value="unknown">unknown</option>
    <option value="high">high</option><option value="moderate">moderate</option>
    <option value="low">low</option></select></label>`;
  for (const s of sc.shelters) {
    html += `<label>Route to ${esc(s.id)} blocked?
      <select data-ev="route_blocked_${esc(s.id)}">
        <option value="unknown">unknown</option><option value="yes">yes</option>
        <option value="no">no</option></select></label>`;
    html += `<label>${esc(s.id)} damaged?
      <select data-ev="damage_${esc(s.id)}">
        <option value="unknown">unknown</option><option value="yes">yes</option>
        <option value="no">no</option></select></label>`;
  }
  $("#bayesControls").innerHTML = html;
}

function gatherEvidence() {
  const ev = {};
  const rain = $("#evRain") ? $("#evRain").value : "unknown";
  if (rain !== "unknown") ev.rain = rain;
  $$("[data-ev]").forEach((sel) => {
    if (sel.value === "yes") ev[sel.dataset.ev] = true;
    else if (sel.value === "no") ev[sel.dataset.ev] = false;
  });
  return ev;
}

$("#btnRunBayes").addEventListener("click", async () => {
  const btn = $("#btnRunBayes");
  btn.disabled = true;
  try {
    const res = await api("/api/bayes", { scenario: state.scenario, evidence: gatherEvidence() });
    state.lastBayes = res;
    renderBayes(res);
    toast("Bayesian inference complete.");
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
});

function renderBayes(res) {
  drawChart("bayes", $("#chartBayes"), {
    type: "bar",
    data: {
      labels: res.shelters.map((s) => s.shelter),
      datasets: [
        { label: "P(accessible) prior", data: res.shelters.map((s) => s.p_accessible_prior), backgroundColor: "#94a3b8", borderRadius: 6 },
        { label: "P(accessible) posterior", data: res.shelters.map((s) => s.p_accessible), backgroundColor: "#0ea5e9", borderRadius: 6 },
      ],
    },
    options: {
      scales: { y: { beginAtZero: true, suggestedMax: 1, ticks: { callback: (v) => Math.round(v * 100) + "%" } } },
      plugins: { title: { display: true, text: `P(demand surge) = ${Math.round(res.demand_surge_probability * 100)}%` } },
    },
  });

  $("#riskBody").innerHTML = res.shelters.map((s) => `<tr>
    <td><b>${esc(s.shelter)}</b> ${esc(s.name)}${s.p_accessible < 0.5 ? ' <span class="tag no">high risk</span>' : ""}</td>
    <td>${fmt(s.capacity)}</td>
    <td>${Math.round(s.p_accessible_prior * 100)}%</td>
    <td><b>${Math.round(s.p_accessible * 100)}%</b></td>
    <td>${fmt(s.risk_adjusted_capacity)}</td>
  </tr>`).join("");

  $("#bayesNarrative").innerHTML = res.narrative.map((n) => `<div>${esc(n)}</div>`).join("");
  const m = res.risk_aware_metrics;
  const risky = res.risky_people
    ? ` · <span class="tag warn">⚠️ ${fmt(res.risky_people)} people via RISKY routes</span>` : "";
  $("#riskPlanSummary").innerHTML =
    `Risk-aware plan (greedy on damage-adjusted capacities): coverage <b>${m.coverage_pct}%</b>, ` +
    `unplaced <b>${fmt(m.unallocated_people)}</b>, overflow <b>${fmt(m.overflow_people)}</b>, ` +
    `score <b>${fmt(m.penalized_score)}</b>${risky}.`;
}

/* ============================================================
   AGENTS TAB
   ============================================================ */
$("#btnRunAgents").addEventListener("click", async () => {
  const btn = $("#btnRunAgents");
  btn.disabled = true;
  try {
    const res = await api("/api/agents", { scenario: state.scenario });
    state.lastAgents = res;
    renderAgents(res);
    toast("Agent coordination finished.");
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
});

function agentClass(name) {
  if (name.startsWith("Coordinator")) return "coord";
  if (name.startsWith("Zone")) return "zone";
  if (name.startsWith("Shelter")) return "shelter";
  if (name.startsWith("Transport")) return "transport";
  return "";
}

function renderAgents(res) {
  $("#agentsSummary").innerHTML = `
    <div class="kpi"><span class="kpi-label">Messages</span><span class="kpi-value">${res.messages}</span></div>
    <div class="kpi"><span class="kpi-label">Escalations</span><span class="kpi-value ${res.escalations ? "bad" : "good"}">${res.escalations}</span></div>
    <div class="kpi"><span class="kpi-label">Coordination time</span><span class="kpi-value">${res.completion_time_min} min</span></div>
    <div class="kpi"><span class="kpi-label">Coverage</span><span class="kpi-value">${res.metrics.coverage_pct}%</span></div>`;

  const log = $("#agentsLog");
  log.innerHTML = res.log.map((l) => `<div class="agent-row ${l.type === "alert" ? "alert" : ""}">
    <span class="agent-time">T+${l.time_min}m</span>
    <span class="agent-name ${agentClass(l.agent)}">${esc(l.agent)}</span>
    <span class="agent-action">${esc(l.action)}</span>
    <span class="agent-detail">${esc(l.detail)}</span>
  </div>`).join("");

  const rows = Array.from(log.querySelectorAll(".agent-row"));
  let i = 0;
  const timer = setInterval(() => {
    const chunk = 3;
    for (let k = 0; k < chunk && i < rows.length; k++, i++) rows[i].classList.add("show");
    if (i >= rows.length) clearInterval(timer);
  }, 40);
}

/* ============================================================
   AI REPORT TAB
   ============================================================ */
$("#btnGenerateReport").addEventListener("click", async () => {
  const btn = $("#btnGenerateReport");
  btn.disabled = true;
  btn.textContent = "⏳ Analysing data…";
  try {
    const res = await api("/api/recommendations", {
      scenario: state.scenario,
      context: {
        comparison: state.lastCompare,
        bayes: state.lastBayes,
        agents: state.lastAgents,
      },
    });
    const badge = $("#reportSource");
    badge.classList.remove("hidden", "gemini", "fallback");
    if (res.source === "gemini") {
      badge.classList.add("gemini");
      badge.textContent = `✨ Gemini (${res.model}) — grounded in computed data`;
    } else {
      badge.classList.add("fallback");
      badge.textContent = "⚙️ Offline template report — add GEMINI_API_KEY to .env for the full AI version";
    }
    const html = typeof marked !== "undefined" ? marked.parse(res.text) : `<pre>${esc(res.text)}</pre>`;
    $("#reportContent").innerHTML = html;
    if (res.error) toast("Gemini call failed: " + res.error, true);
  } catch (err) { toast(err.message, true); }
  btn.disabled = false;
  btn.textContent = "✨ Generate report";
});

/* ----- chat ----- */
const CHAT_CHIPS = [
  "Which shelter is most at risk and why?",
  "Why did Greedy lose to Branch & Bound?",
  "Which shelters are near capacity?",
  "What should we do in the next 24 hours?",
];

function renderChatChips() {
  $("#chatChips").innerHTML = CHAT_CHIPS.map((c) =>
    `<button class="qchip">${esc(c)}</button>`).join("");
}

function renderChat() {
  $("#chatBox").innerHTML = state.chat.map((m) => {
    if (m.role === "assistant" && m.source) {
      const tag = m.source === "gemini"
        ? '<span class="src gemini">✨ Gemini analyst</span>'
        : '<span class="src fallback">⚙️ offline analyst</span>';
      return `<div class="bubble assistant">${esc(m.content)}${tag}</div>`;
    }
    return `<div class="bubble ${m.role === "user" ? "user" : "assistant"}">${esc(m.content)}</div>`;
  }).join("");
  const box = $("#chatBox");
  box.scrollTop = box.scrollHeight;
}

async function sendChat() {
  const input = $("#chatInput");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  state.chat.push({ role: "user", content: text });
  renderChat();
  const box = $("#chatBox");
  box.insertAdjacentHTML("beforeend",
    `<div class="bubble assistant typing" id="chatTyping"><span></span><span></span><span></span></div>`);
  box.scrollTop = box.scrollHeight;
  $("#btnChatSend").disabled = true;
  try {
    const res = await api("/api/chat", {
      messages: state.chat,
      scenario: state.scenario,
      context: {
        comparison: state.lastCompare,
        bayes: state.lastBayes,
        agents: state.lastAgents,
        allocation: state.lastSolve ? state.lastSolve.metrics.allocation : null,
      },
    });
    state.chat.push({ role: "assistant", content: res.text, source: res.source });
    if (res.error) toast("Gemini unavailable — offline analyst answered.", true);
  } catch (err) {
    state.chat.push({ role: "assistant", content: "⚠️ " + err.message, source: "fallback" });
  }
  const t = document.getElementById("chatTyping");
  if (t) t.remove();
  renderChat();
  $("#btnChatSend").disabled = false;
}

$("#chatChips").addEventListener("click", (e) => {
  const chip = e.target.closest(".qchip");
  if (!chip) return;
  $("#chatInput").value = chip.textContent;
  sendChat();
});

$("#btnChatClear").addEventListener("click", () => {
  state.chat = [];
  renderChat();
});

$("#btnChatSend").addEventListener("click", sendChat);
$("#chatInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});

/* ============================================================
   INIT
   ============================================================ */
(async function init() {
  try {
    const health = await api("/api/health");
    $("#chipGemini").innerHTML = health.gemini_configured
      ? `✨ <b>Gemini</b> connected (${esc(health.gemini_model)})`
      : `⚙️ <b>Gemini offline</b> — template mode`;
  } catch (err) {
    toast(err.message, true);
    return;
  }
  try {
    state.scenario = await api("/api/scenario");
    renderScenarioEditor();
    renderBayesControls();
    renderChatChips();
    renderChat();
    state.lastSolve = await api("/api/solve", { algo: "greedy", scenario: state.scenario });
    renderDashboard();
  } catch (err) {
    toast(err.message, true);
  }
})();
