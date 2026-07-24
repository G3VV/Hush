/* Hush — a live map of how Bristol moves: shared scooters and bikes, buses,
   coaches and trains, on one map.
   Everything under /api is served by hush/server.py. History is accumulated
   locally from repeated polls, so it starts empty and fills in. */

'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const state = {
  vehicles: [],
  buses: [],
  trains: [],
  filters: { type: '', minFuel: 0 },
  colorBy: 'battery',
  selected: null,
  windowHours: 24,
  stats: null,
  fleetView: 'busiest',
  map: null,
  tiles: null,
  renderer: null,
  layers: {},
  markers: new Map(),
};

/* ── formatting ─────────────────────────────────────────────────────── */

const pad = n => String(n).padStart(2, '0');

function fmtDur(s) {
  if (s == null) return '—';
  s = Math.max(0, Math.round(s));
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ' + pad(m % 60) + 'm';
  return Math.floor(h / 24) + 'd ' + (h % 24) + 'h';
}

function fmtDist(m) {
  if (m == null) return '—';
  return m < 1000 ? Math.round(m) + ' m' : (m / 1000).toFixed(2) + ' km';
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const today = new Date();
  const sameDay = d.toDateString() === today.toDateString();
  const t = pad(d.getHours()) + ':' + pad(d.getMinutes());
  return sameDay ? t : `${d.getDate()}/${d.getMonth() + 1} ${t}`;
}

function fmtNum(n, dp = 0) {
  if (n == null || Number.isNaN(n)) return '—';
  return Number(n).toLocaleString('en-GB', { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

function toast(msg, ms = 2600) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove('show'), ms);
}

async function api(path) {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

const css = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/* ── colour encodings ───────────────────────────────────────────────── */
/* Battery and idle are magnitudes shown in banded steps; the exact value is
   always available in the tooltip and the detail panel, so colour never
   carries the meaning alone. */

const ENCODINGS = {
  battery: {
    title: 'Battery',
    of: v => (v.f >= 60 ? 0 : v.f >= 25 ? 1 : 2),
    colors: () => [css('--bat-high'), css('--bat-mid'), css('--bat-low')],
    labels: ['60% and above', '25–59%', 'Below 25%'],
  },
  type: {
    title: 'Vehicle type',
    of: v => (v.t === 1 ? 1 : 0),
    colors: () => [css('--series-1'), css('--series-2')],
    labels: ['Scooter', 'Bike'],
  },
  idle: {
    title: 'Time since moved',
    of: v => (v.idle < 6 * 3600 ? 0 : v.idle < 24 * 3600 ? 1 : 2),
    colors: () => [css('--series-1'), css('--bat-mid'), css('--bat-low')],
    labels: ['Under 6h', '6–24h', 'Over 24h'],
  },
};

function colorOf(v) {
  const enc = ENCODINGS[state.colorBy];
  return enc.colors()[enc.of(v)];
}

function renderLegend() {
  const enc = ENCODINGS[state.colorBy];
  const cols = enc.colors();
  $('#legend').innerHTML =
    `<div class="ctl-label">${enc.title}</div>` +
    enc.labels.map((l, i) =>
      `<div class="legend-row"><span class="legend-swatch" style="background:${cols[i]}"></span>${l}</div>`
    ).join('') +
    /* Mode legend. Each transport type differs in shape, size and colour at
       once, so the three cues reinforce rather than compete. */
    `<div class="ctl-label" style="margin-top:12px">Transport type</div>
     <div class="legend-row"><span class="legend-mode-scooter"></span>Small dot — scooter or bike</div>
     <div class="legend-row"><span class="legend-shape-arrow"></span>Arrow — bus, pointing its way</div>
     <div class="legend-row"><span class="legend-shape-train"></span>Bar — train (estimated position)</div>
     <div class="legend-row"><span class="legend-shape-square"></span>Outlined square — station</div>`;
}

/* ── map ────────────────────────────────────────────────────────────── */

const TILES = {
  dark:  'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  light: 'https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png',
};
const ATTR = '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> ' +
             '&copy; <a href="https://carto.com/attributions">CARTO</a> · ' +
             'scooters: Dott · buses: DfT BODS · trains: Realtime Trains';

function initMap(center, zoom) {
  /* One shared canvas for every vector layer. If layers are left to create
     their own, Leaflet stacks a second canvas over the vehicles one, and the
     upper canvas swallows clicks meant for the markers underneath — which
     silently breaks scooter clicks the moment anything draws a trail. */
  state.renderer = L.canvas({ padding: 0.5 });
  /* Zoom sits bottom-right so it never collides with the control panel. */
  state.map = L.map('map', {
    zoomControl: false,
    preferCanvas: true,
    renderer: state.renderer,
  }).setView(center, zoom);
  L.control.zoom({ position: 'bottomright' }).addTo(state.map);
  setTiles();

  state.layers.vehicles = L.layerGroup().addTo(state.map);
  state.layers.trail    = L.layerGroup().addTo(state.map);
  state.layers.zones     = L.layerGroup();
  state.layers.stations  = L.layerGroup();
  state.layers.pickups   = L.layerGroup();
  state.layers.dropoffs  = L.layerGroup();
  state.layers.balance   = L.layerGroup();
  state.layers.buses     = L.layerGroup().addTo(state.map);
  state.layers.trains    = L.layerGroup().addTo(state.map);
  state.layers.rail      = L.layerGroup().addTo(state.map);
  state.layers.stops     = L.layerGroup();
  state.layers.ferry     = L.layerGroup();

  state.map.on('click', e => {
    if (!e.originalEvent.target.closest('.leaflet-interactive')) closeDetail();
  });
}

function setTiles() {
  const theme = document.documentElement.getAttribute('data-theme');
  if (state.tiles) state.map.removeLayer(state.tiles);
  state.tiles = L.tileLayer(TILES[theme] || TILES.dark, {
    attribution: ATTR, subdomains: 'abcd', maxZoom: 20,
  }).addTo(state.map);
}

function radiusForZoom() {
  /* Scooters are by far the most numerous mark, so they are kept small and
     read as a background field; buses and trains sit above them. */
  const z = state.map.getZoom();
  return z >= 17 ? 7 : z >= 15 ? 5 : z >= 13 ? 3.5 : 2.5;
}

function drawVehicles() {
  const grp = state.layers.vehicles;
  grp.clearLayers();
  state.markers.clear();

  const r = radiusForZoom();
  const strokeCol = css('--surface-1');

  for (const v of state.vehicles) {
    const m = L.circleMarker([v.lat, v.lon], {
      renderer: state.renderer,
      radius: v.id === state.selected ? r + 3.5 : r,
      /* a 2px surface ring keeps overlapping marks separable */
      color: v.id === state.selected ? css('--text-primary') : strokeCol,
      weight: v.id === state.selected ? 2.5 : 1.5,
      fillColor: colorOf(v),
      fillOpacity: v.d ? 0.3 : 0.8,
      bubblingMouseEvents: false,
    });
    m.bindTooltip(
      `<b>${v.f}%</b> · ${v.t === 1 ? 'bike' : 'scooter'}<br>idle ${fmtDur(v.idle)}` +
      (v.trips ? `<br>${v.trips} ride${v.trips === 1 ? '' : 's'} seen` : ''),
      { direction: 'top', offset: [0, -4], opacity: 0.96 }
    );
    m.on('click', () => selectVehicle(v.id));
    grp.addLayer(m);
    state.markers.set(v.id, m);
  }

  $('#countLabel').textContent =
    `${fmtNum(state.vehicles.length)} vehicles shown`;
}

/* ── live data ──────────────────────────────────────────────────────── */

async function loadLive() {
  const q = new URLSearchParams();
  if (state.filters.type) q.set('type', state.filters.type);
  if (state.filters.minFuel > 0) q.set('min_fuel', state.filters.minFuel);
  try {
    const data = await api('/api/live?' + q);
    state.vehicles = data.vehicles;
    drawVehicles();
  } catch (e) {
    toast('Could not load vehicles: ' + e.message);
  }
}

async function loadHealth() {
  try {
    const h = await api('/api/health');
    const dot = $('#liveDot'), txt = $('#liveText');
    const last = h.last_poll;
    const age = last ? h.now - last.ts : null;

    if (!last) {
      dot.className = 'dot warn';
      txt.textContent = 'waiting for first poll';
    } else if (age > h.poll_interval_s * 3) {
      dot.className = 'dot bad';
      txt.textContent = `stale · ${fmtDur(age)} ago`;
    } else {
      dot.className = 'dot ok';
      txt.textContent = `${fmtNum(h.rentals)} rentals tracked · updated ${fmtDur(age)} ago`;
    }
    return h;
  } catch {
    $('#liveDot').className = 'dot bad';
    $('#liveText').textContent = 'server unreachable';
    return null;
  }
}

/* ── vehicle detail + history trail ─────────────────────────────────── */

async function selectVehicle(id) {
  state.selected = id;
  drawVehicles();
  const box = $('#detail');
  box.classList.add('is-open');
  $('#detailBody').innerHTML = '<div class="detail-inner"><p class="muted">Loading…</p></div>';

  let v;
  try {
    v = await api('/api/vehicle/' + encodeURIComponent(id));
  } catch (e) {
    $('#detailBody').innerHTML = `<div class="detail-inner"><p class="muted">Could not load: ${e.message}</p></div>`;
    return;
  }
  renderDetail(v);
  drawTrail(v);
}

function closeDetail() {
  state.selected = null;
  $('#detail').classList.remove('is-open');
  state.layers.trail.clearLayers();
  drawVehicles();
}

const KIND_LABEL = {
  relocation: 'Moved by van', service: 'Battery swap / depot', trip: 'Moved', drift: 'GPS drift',
};
const KIND_CLASS = { trip: 'is-trip', service: 'is-service', relocation: 'is-reloc' };

function renderDetail(v) {
  const isBike = v.type === 'dott_bicycle';
  const fuel = Math.round((v.fuel || 0) * 100);
  const fuelCol = fuel >= 60 ? css('--bat-high') : fuel >= 25 ? css('--bat-mid') : css('--bat-low');
  const nowS = Math.floor(Date.now() / 1000);

  const badges = [
    v.present
      ? `<span class="badge"><span class="legend-swatch" style="background:${css('--series-3')}"></span>Available</span>`
      : `<span class="badge"><span class="legend-swatch" style="background:${css('--series-2')}"></span>Gone — rented or collected</span>`,
  ];
  if (v.is_disabled) badges.push('<span class="badge">Disabled</span>');
  if (v.is_reserved) badges.push('<span class="badge">Reserved</span>');

  /* Merge stays and operational moves into one reverse-chronological history. */
  const events = [];
  for (const m of v.moves) {
    events.push({
      ts: m.end_ts, kind: m.kind,
      title: KIND_LABEL[m.kind] || m.kind,
      meta: `${fmtDist(m.distance_m)} · ${fmtTime(m.end_ts)}`,
      lat: m.end_lat, lon: m.end_lon,
    });
  }
  for (const s of v.stays) {
    const dur = (s.end_ts || nowS) - s.start_ts;
    const drop = (s.start_fuel != null && s.end_fuel != null)
      ? ` · battery ${Math.round(s.start_fuel * 100)}%→${Math.round(s.end_fuel * 100)}%` : '';
    events.push({
      ts: s.start_ts, kind: 'stay',
      title: s.is_open ? 'Parked here (still here)' : 'Parked',
      meta: `${fmtDur(dur)} · from ${fmtTime(s.start_ts)}${drop}`,
      lat: s.lat, lon: s.lon,
    });
  }
  events.sort((a, b) => b.ts - a.ts);

  const history = events.length
    ? `<div class="timeline">` + events.slice(0, 40).map(e =>
        `<div class="tl-item ${KIND_CLASS[e.kind] || ''}" data-lat="${e.lat}" data-lon="${e.lon}">
           <div class="tl-title">${e.title}</div>
           <div class="tl-meta">${e.meta}</div>
         </div>`).join('') + `</div>`
    : `<div class="empty">Nothing recorded for this ID yet — it appeared on the
        last poll. Its history builds up from here.</div>`;

  $('#detailBody').innerHTML = `
    <div class="detail-inner">
      <div class="veh-head">
        <div class="veh-type">${isBike ? 'E-bike' : 'E-scooter'}</div>
        <p class="veh-id">${v.id}</p>
        <div class="badges">${badges.join('')}</div>
      </div>

      <div class="kv">
        <div class="kv-item"><div class="kv-k">Battery</div>
          <div class="kv-v" style="color:${fuelCol}">${fuel}<small>%</small></div></div>
        <div class="kv-item"><div class="kv-k">Range</div>
          <div class="kv-v">${((v.range_m || 0) / 1000).toFixed(1)}<small>km</small></div></div>
        <div class="kv-item"><div class="kv-k">${v.present ? 'Parked for' : 'Gone for'}</div>
          <div class="kv-v" style="font-size:14px">${fmtDur(v.present ? v.idle_s : nowS - v.gone_since)}</div></div>
        <div class="kv-item"><div class="kv-k">Tracked for</div>
          <div class="kv-v" style="font-size:14px">${fmtDur(v.tracked_s)}</div></div>
        <div class="kv-item"><div class="kv-k">Battery drain</div>
          <div class="kv-v" style="font-size:14px">${v.battery_drain_pct_per_h != null ? v.battery_drain_pct_per_h + '<small>%/h</small>' : '—'}</div></div>
        <div class="kv-item"><div class="kv-k">Ops moves</div>
          <div class="kv-v">${v.move_count}</div></div>
      </div>

      <div class="sub-head"><span>History of this ID</span><span>${events.length} events</span></div>
      ${history}

      <a class="btn-link" href="${v.rental_uri}" target="_blank" rel="noopener">Open in Dott ↗</a>
      <p class="muted" style="margin-top:10px">
        Dott issues a fresh ID each time a vehicle is rented, so this history covers
        the current parked spell only — where it is, how long it has sat, how the
        battery is draining, and any moves by the operations team. Once someone
        rents it, this ID retires and the vehicle reappears as a new one.
      </p>
    </div>`;

  $$('.tl-item', $('#detailBody')).forEach(el => {
    el.addEventListener('click', () => {
      const lat = parseFloat(el.dataset.lat), lon = parseFloat(el.dataset.lon);
      if (!Number.isNaN(lat)) state.map.flyTo([lat, lon], 17, { duration: 0.6 });
    });
  });
}

function drawTrail(v) {
  const g = state.layers.trail;
  g.clearLayers();

  const kindColor = {
    trip: css('--series-1'), relocation: css('--series-2'), service: css('--series-3'),
  };

  const trips = (v.moves || []).filter(t => t.kind !== 'drift').slice(0, 40);
  for (const t of trips) {
    const col = kindColor[t.kind] || css('--text-muted');
    L.polyline([[t.start_lat, t.start_lon], [t.end_lat, t.end_lon]], {
      color: col, weight: 2, opacity: 0.85,
      dashArray: t.kind === 'trip' ? null : '5,5',
    }).addTo(g);
    L.circleMarker([t.start_lat, t.start_lon], {
      radius: 4, color: css('--surface-1'), weight: 1.5,
      fillColor: col, fillOpacity: 0.9,
    }).bindTooltip(`Started ${fmtTime(t.start_ts)}`, { direction: 'top' }).addTo(g);
  }

  if (v.lat != null) {
    L.circleMarker([v.lat, v.lon], {
      radius: 9, color: css('--text-primary'), weight: 2.5,
      fillColor: colorOf({ f: Math.round((v.fuel || 0) * 100), t: v.type === 'dott_bicycle' ? 1 : 0, idle: v.idle_s || 0 }),
      fillOpacity: 1,
    }).addTo(g);
    state.map.flyTo([v.lat, v.lon], Math.max(state.map.getZoom(), 16), { duration: 0.6 });
  }

  if (trips.length) {
    const pts = trips.flatMap(t => [[t.start_lat, t.start_lon], [t.end_lat, t.end_lon]]);
    setTimeout(() => state.map.fitBounds(L.latLngBounds(pts).pad(0.25), { maxZoom: 16 }), 700);
  }
}

/* ── public transport ───────────────────────────────────────────────────
   Mode is carried by SHAPE, not colour: buses are arrows, scooters are
   circles, infrastructure is a square in neutral ink. That keeps the colour
   channel free to mean battery level and nothing else, and it keeps the
   aqua/red pair separable for colour-blind readers. */

async function loadBuses() {
  let data;
  try {
    data = await api('/api/transit');
  } catch (e) {
    toast('Bus feed unavailable: ' + e.message);
    return;
  }
  state.buses = data.vehicles;
  drawBuses();
  $('#busCount').textContent = data.vehicles.length ? `(${fmtNum(data.vehicles.length)})` : '';
}

function drawBuses() {
  const g = state.layers.buses;
  g.clearLayers();
  const col = css('--bus-body');

  for (const b of state.buses) {
    const known = b.brg != null;
    // A bus with a known heading gets a pointed arrow; without one it gets a
    // neutral dot, so the shape never implies a direction we do not have.
    const html = known
      ? `<div class="bus-arrow" style="transform:rotate(${b.brg}deg)"></div>`
      : `<div class="bus-dot"></div>`;
    const m = L.marker([b.lat, b.lon], {
      icon: L.divIcon({ className: 'bus-icon', html, iconSize: [16, 16], iconAnchor: [8, 8] }),
      keyboard: false,
    });
    m.bindTooltip(
      `<b>${b.line ? 'Route ' + b.line : 'Bus'}</b>` +
      (b.dest ? ` → ${b.dest}` : '') + `<br>${b.op || 'unknown operator'}` +
      `<br>${b.kmh != null ? b.kmh + ' km/h' : 'speed unknown'} · reported ${fmtDur(b.age)} ago`,
      { direction: 'top', offset: [0, -6] });
    m.on('click', () => selectBus(b.id));
    g.addLayer(m);
  }
}

async function selectBus(id) {
  state.selected = null;
  const box = $('#detail');
  box.classList.add('is-open');
  $('#detailBody').innerHTML = '<div class="detail-inner"><p class="muted">Loading…</p></div>';
  let v;
  try {
    v = await api('/api/transit/vehicle/' + encodeURIComponent(id));
  } catch (e) {
    $('#detailBody').innerHTML = `<div class="detail-inner"><p class="muted">Could not load: ${e.message}</p></div>`;
    return;
  }
  renderBusDetail(v);
  drawBusTrack(v);
}

function renderBusDetail(v) {
  const track = v.track || [];
  const rows = track.slice().reverse().slice(0, 40).map(p =>
    `<div class="tl-item is-trip" data-lat="${p.lat}" data-lon="${p.lon}">
       <div class="tl-title">${p.speed_kmh != null ? p.speed_kmh.toFixed(1) + ' km/h' : 'Position fix'}</div>
       <div class="tl-meta">${fmtTime(p.ts)}${p.bearing != null ? ' · heading ' + Math.round(p.bearing) + '°' : ''}</div>
     </div>`).join('');

  $('#detailBody').innerHTML = `
    <div class="detail-inner">
      <div class="veh-head">
        <div class="veh-type">${v.line_name ? 'Route ' + v.line_name : 'Bus'}${v.destination_name ? ' → ' + v.destination_name : ''}</div>
        <p class="veh-id">${v.id}</p>
        <div class="badges">
          <span class="badge"><span class="legend-swatch" style="background:${css('--series-3')}"></span>${v.operator || 'Unknown operator'}</span>
          ${v.age_s > 300 ? '<span class="badge">Position going stale</span>' : ''}
        </div>
      </div>

      <div class="kv">
        <div class="kv-item"><div class="kv-k">Speed now</div>
          <div class="kv-v">${v.speed_kmh != null ? v.speed_kmh.toFixed(1) + '<small>km/h</small>' : '—'}</div></div>
        <div class="kv-item"><div class="kv-k">Average</div>
          <div class="kv-v">${v.avg_speed_kmh != null ? v.avg_speed_kmh + '<small>km/h</small>' : '—'}</div></div>
        <div class="kv-item"><div class="kv-k">Tracked for</div>
          <div class="kv-v" style="font-size:14px">${fmtDur(v.tracked_s)}</div></div>
        <div class="kv-item"><div class="kv-k">Distance</div>
          <div class="kv-v" style="font-size:14px">${fmtDist(v.distance_m)}</div></div>
        <div class="kv-item"><div class="kv-k">Position age</div>
          <div class="kv-v" style="font-size:14px">${fmtDur(v.age_s)}</div></div>
        <div class="kv-item"><div class="kv-k">Fixes</div>
          <div class="kv-v">${fmtNum(v.fixes)}</div></div>
      </div>

      <div class="sub-head"><span>Recorded track</span><span>${track.length} fixes</span></div>
      ${track.length > 1
        ? `<div class="timeline">${rows}</div>`
        : `<div class="empty">Only one fix so far. Bus fleet numbers are stable, so a
             real trail builds up from here — check back in a few minutes.</div>`}

      <p class="muted" style="margin-top:12px">
        The solid line is where this bus has actually been; the dashed line is the
        rest of its route from the operator's timetable — where it is routed to go,
        not a prediction of when it arrives. Speed is derived between polls, as the
        feed carries no speed field.
      </p>
    </div>`;

  $$('.tl-item', $('#detailBody')).forEach(el => {
    el.addEventListener('click', () => {
      const lat = parseFloat(el.dataset.lat), lon = parseFloat(el.dataset.lon);
      if (!Number.isNaN(lat)) state.map.flyTo([lat, lon], 17, { duration: 0.6 });
    });
  });
}

async function drawBusTrack(v) {
  const g = state.layers.trail;
  g.clearLayers();

  /* Past is measured — positions actually recorded. Future is the route
     alignment from the operator's timetable, so it is where the bus is routed
     to go rather than a prediction of when it gets there. Dashed says that. */
  let path = null;
  try { path = await api('/api/transit/path/' + encodeURIComponent(v.id)); }
  catch (e) { /* fall back to the recorded track alone */ }

  if (path && path.future && path.future.length > 1) {
    L.polyline(path.future, {
      color: css('--bus-body'), weight: 3, opacity: 0.55, dashArray: '7,6',
    }).bindTooltip('Route ahead' + (path.destination ? ' → ' + path.destination : ''),
                   { sticky: true }).addTo(g);
  }

  const pts = (path && path.past && path.past.length > 1)
    ? path.past
    : (v.track || []).filter(p => p.lat != null).map(p => [p.lat, p.lon]);

  if (pts.length > 1) {
    L.polyline(pts, { color: css('--bus-body'), weight: 4, opacity: 0.95 })
      .bindTooltip('Travelled so far', { sticky: true }).addTo(g);
    L.circleMarker(pts[0], {
      renderer: state.renderer, radius: 5, color: css('--halo'), weight: 2,
      fillColor: css('--bus-body'), fillOpacity: 1,
    }).bindTooltip('Track starts here', { direction: 'top' }).addTo(g);
  }

  if (v.lat != null) {
    L.circleMarker([v.lat, v.lon], {
      renderer: state.renderer, radius: 8, color: css('--text-primary'), weight: 2.5,
      fillColor: css('--bus-body'), fillOpacity: 1,
    }).addTo(g);
  }

  const all = (path && path.future ? path.future : []).concat(pts);
  if (all.length > 1) state.map.fitBounds(L.latLngBounds(all).pad(0.2), { maxZoom: 15 });
  else if (v.lat != null) state.map.flyTo([v.lat, v.lon], 16, { duration: 0.6 });
}

/* ── rail ───────────────────────────────────────────────────────────────
   Realtime Trains gives timings, not coordinates, so there are no moving
   train markers. Stations become live instead: click one for a real board. */

async function loadRail(layer) {
  const { stations } = await api('/api/rail');
  if (!stations.length) {
    toast('No rail stations matched yet — needs an RTT token and one poll');
    return;
  }
  for (const s of stations) {
    const busy = s.services > 0;
    /* Filled = services due, strongly filled = running late. No hue: see CSS. */
    const late = s.avg_delay != null && s.avg_delay > 5;
    L.marker([s.lat, s.lon], {
      icon: L.divIcon({
        className: 'infra-icon',
        html: `<div class="infra-mark is-rail${busy ? ' is-live' : ''}${late ? ' is-late' : ''}"></div>`,
        iconSize: [13, 13], iconAnchor: [6.5, 6.5],
      }),
      keyboard: false,
    }).bindTooltip(
      `<b>${s.name}</b><br>${s.services} service${s.services === 1 ? '' : 's'} due` +
      (s.avg_delay != null ? `<br>avg ${s.avg_delay > 0 ? '+' : ''}${s.avg_delay} min` : ''),
      { direction: 'top', offset: [0, -6] })
      .on('click', () => selectStation(s.code))
      .addTo(layer);
  }
}

/* Trains are ESTIMATED positions, not GPS. They get their own shape — a
   rounded bar, like a carriage — and a dashed halo when the estimate is
   weaker, so they are never mistaken for the measured bus positions. */
async function loadTrains() {
  let data;
  try {
    data = await api('/api/trains');
  } catch (e) {
    return;
  }
  state.trains = data.trains;
  drawTrains();
  $('#trainCount').textContent = data.trains.length ? `(${fmtNum(data.trains.length)})` : '';
}

function drawTrains() {
  const g = state.layers.trains;
  g.clearLayers();
  for (const t of state.trains) {
    const late = t.delay != null && t.delay > 5;
    /* Body colour is the mode's identity and never changes; lateness is a
       ring around it, so a delayed train still reads as a train. */
    const cls = 'train-mark' + (t.state === 'at_station' ? ' is-stopped' : '') +
                (t.on_track ? '' : ' is-offtrack') + (late ? ' is-late' : '');
    const m = L.marker([t.lat, t.lon], {
      icon: L.divIcon({
        className: 'train-icon',
        html: `<div class="${cls}"${t.brg != null ? ` style="transform:rotate(${t.brg}deg)"` : ''}></div>`,
        iconSize: [18, 22], iconAnchor: [9, 11],
      }),
      zIndexOffset: 500,
      keyboard: false,
    });
    m.bindTooltip(
      `<b>${t.code || 'train'}</b> ${t.op || ''}<br>` +
      `${t.from || '?'} → ${t.to || '?'}<br>` +
      (t.state === 'at_station'
        ? 'At the station'
        : `${Math.round(t.progress * 100)}% along this leg`) +
      (t.delay != null ? `<br>${t.delay > 0 ? '+' + t.delay + ' min' : 'on time'}` : '') +
      `<span class="t-sub">estimated from timings, not GPS</span>`,
      { direction: 'top', offset: [0, -8] });
    m.on('click', () => selectTrain(t));
    g.addLayer(m);
  }
}

/* Journey geometry: where it has been (solid) and where it is going
   (dashed). Both follow real track, so they trace the actual line. */
async function drawTrainPath(uid) {
  const g = state.layers.trail;
  g.clearLayers();
  let d;
  try {
    d = await api('/api/trains/' + encodeURIComponent(uid) + '/path');
  } catch (e) { return; }

  if (d.future && d.future.length > 1) {
    L.polyline(d.future, {
      color: css('--series-1'), weight: 3, opacity: 0.75, dashArray: '7,6',
    }).bindTooltip('Still to come', { sticky: true }).addTo(g);
  }
  if (d.past && d.past.length > 1) {
    L.polyline(d.past, {
      color: css('--train-body'), weight: 4, opacity: 0.95,
    }).bindTooltip('Travelled so far', { sticky: true }).addTo(g);
  }
  /* Calling points along the way, so the line reads as a journey. */
  for (const c of d.calls || []) {
    if (c.lat == null) continue;
    L.circleMarker([c.lat, c.lon], {
      renderer: state.renderer, radius: 3.5,
      color: css('--train-edge'), weight: 1.5,
      fillColor: css('--train-body'), fillOpacity: 1,
    }).bindTooltip(
      `<b>${c.name || c.code}</b>` + (c.arr ? `<span class="t-sub">${fmtTime(c.arr)}</span>` : ''),
      { direction: 'top' }).addTo(g);
  }
  const all = (d.past || []).concat(d.future || []);
  if (all.length > 1) state.map.fitBounds(L.latLngBounds(all).pad(0.15));
}

function selectTrain(t) {
  $('#detail').classList.add('is-open');
  const pct = Math.round((t.progress || 0) * 100);
  $('#detailBody').innerHTML = `
    <div class="detail-inner">
      <div class="veh-head">
        <div class="veh-type">Train · ${t.code || '—'}</div>
        <p class="veh-id" style="font-family:var(--sans);font-size:15px">${t.origin || '?'} → ${t.destination || '?'}</p>
        <div class="badges">
          <span class="badge">${t.op || 'Unknown operator'}</span>
          <span class="badge">${t.state === 'at_station' ? 'At station' : 'Between stations'}</span>
          ${t.basis === 'actual'
            ? '<span class="badge">From observed times</span>'
            : '<span class="badge">From forecast times</span>'}
        </div>
      </div>

      <div class="kv">
        <div class="kv-item"><div class="kv-k">Delay</div>
          <div class="kv-v" style="color:${t.delay > 5 ? css('--bat-low') : css('--series-3')}">
            ${t.delay == null ? '—' : (t.delay > 0 ? '+' : '') + t.delay + '<small>min</small>'}</div></div>
        <div class="kv-item"><div class="kv-k">Along this leg</div>
          <div class="kv-v">${pct}<small>%</small></div></div>
      </div>

      <div class="sub-head"><span>Current leg</span></div>
      <div class="leg">
        <div class="leg-end"><div class="leg-name">${t.from || '—'}</div>
          <div class="leg-time">${t.leg_start ? fmtTime(t.leg_start) : ''}</div></div>
        <div class="leg-bar"><div class="leg-fill" style="width:${pct}%"></div></div>
        <div class="leg-end leg-right"><div class="leg-name">${t.to || '—'}</div>
          <div class="leg-time">${t.leg_end ? fmtTime(t.leg_end) : ''}</div></div>
      </div>

      <p class="muted" style="margin-top:14px">
        <strong>This position is an estimate.</strong> Realtime Trains publishes no
        coordinates, so the train is placed by interpolating between its calling
        points using ${t.basis === 'actual' ? 'the times it actually passed them' : 'forecast times'},
        then ${t.on_track
          ? 'snapping the result onto OpenStreetMap railway geometry'
          : 'left unsnapped — no mapped track was near enough to be confident'}.
        Accuracy depends on how far apart the calling points are: a train between
        two distant ones can be a fair way off.
      </p>
    </div>`;
  state.layers.trail.clearLayers();
  drawTrainPath(t.uid);
}

async function selectStation(code) {
  const box = $('#detail');
  box.classList.add('is-open');
  $('#detailBody').innerHTML = '<div class="detail-inner"><p class="muted">Loading board…</p></div>';
  let d;
  try {
    d = await api('/api/rail/station/' + encodeURIComponent(code));
  } catch (e) {
    $('#detailBody').innerHTML = `<div class="detail-inner"><p class="muted">Could not load: ${e.message}</p></div>`;
    return;
  }
  renderBoard(d);
  state.layers.trail.clearLayers();
  state.map.flyTo([d.station.lat, d.station.lon], 15, { duration: 0.6 });
}

function renderBoard(d) {
  const st = d.station;
  const upcoming = d.services.filter(s => !s.is_cancelled);
  const delays = upcoming.map(s => s.delay_min).filter(v => v != null);
  const avg = delays.length ? (delays.reduce((a, b) => a + b, 0) / delays.length) : null;

  const rows = d.services.length ? d.services.map(s => {
    const t = s.scheduled_ts ? fmtTime(s.scheduled_ts) : '--:--';
    let status, cls;
    if (s.is_cancelled) { status = 'Cancelled'; cls = 'is-cancelled'; }
    else if (s.delay_min == null) { status = 'No forecast'; cls = ''; }
    else if (s.delay_min <= 0) { status = 'On time'; cls = 'is-ontime'; }
    else if (s.delay_min <= 5) { status = `+${s.delay_min} min`; cls = 'is-ontime'; }
    else { status = `+${s.delay_min} min`; cls = 'is-late'; }
    return `<div class="board-row">
        <div class="board-time">${t}</div>
        <div class="board-mid">
          <div class="board-dest">${s.destination || '—'}</div>
          <div class="board-sub">${s.operator || ''}${s.platform ? ' · plat ' + s.platform : ''}${s.headcode ? ' · ' + s.headcode : ''}</div>
        </div>
        <div class="board-status ${cls}">${status}</div>
      </div>`;
  }).join('') : `<div class="empty">No services on the board right now.</div>`;

  $('#detailBody').innerHTML = `
    <div class="detail-inner">
      <div class="veh-head">
        <div class="veh-type">Rail station · ${st.code}</div>
        <p class="veh-id" style="font-family:var(--sans);font-size:15px">${st.name}</p>
      </div>

      <div class="kv">
        <div class="kv-item"><div class="kv-k">Services due</div>
          <div class="kv-v">${upcoming.length}</div></div>
        <div class="kv-item"><div class="kv-k">Average delay</div>
          <div class="kv-v" style="color:${avg == null ? 'inherit' : avg > 5 ? css('--bat-low') : css('--series-3')}">
            ${avg == null ? '—' : (avg > 0 ? '+' : '') + avg.toFixed(1) + '<small>min</small>'}</div></div>
      </div>

      <div class="sub-head"><span>Live board</span><span>${d.services.length} services</span></div>
      <div class="board">${rows}</div>

      <p class="muted" style="margin-top:12px">
        Live timings from Realtime Trains. That feed carries no coordinates for
        trains, so services are shown against the stations they call at rather
        than as moving markers on the map.
      </p>
    </div>`;
}

/* Infrastructure: squares in neutral ink, so it never competes with vehicles. */
async function loadInfrastructure(kinds, layer, label) {
  const q = kinds.map(k => 'kind=' + encodeURIComponent(k)).join('&');
  const { features } = await api('/api/infrastructure?' + q);
  if (!features.length) {
    toast(`No ${label} cached yet — OpenStreetMap lookup may still be pending`);
    return;
  }
  const big = kinds[0] === 'rail_station';
  for (const f of features) {
    L.marker([f.lat, f.lon], {
      icon: L.divIcon({
        className: 'infra-icon',
        html: `<div class="infra-mark ${big ? 'is-rail' : 'is-small'}"></div>`,
        iconSize: big ? [11, 11] : [7, 7],
        iconAnchor: big ? [5.5, 5.5] : [3.5, 3.5],
      }),
      keyboard: false,
    }).bindTooltip(f.name || label, { direction: 'top', offset: [0, -5] }).addTo(layer);
  }
}

/* ── optional layers ────────────────────────────────────────────────── */

async function toggleLayer(key, on, loader) {
  const layer = state.layers[key];
  if (!on) { state.map.removeLayer(layer); return; }
  if (!layer.getLayers().length) {
    try { await loader(layer); }
    catch (e) { toast('Layer failed: ' + e.message); return; }
  }
  layer.addTo(state.map);
}

async function loadZones(layer) {
  const geo = await api('/api/zones');
  L.geoJSON(geo, {
    style: () => ({
      color: css('--bat-low'), weight: 1.2, opacity: 0.75,
      fillColor: css('--bat-low'), fillOpacity: 0.12,
    }),
    onEachFeature: (f, l) => {
      const r = (f.properties?.rules || [])[0] || {};
      l.bindTooltip(
        r.ride_allowed === false ? 'No-ride zone' : 'Restricted zone',
        { sticky: true });
    },
  }).addTo(layer);
}

async function loadStations(layer) {
  const { stations } = await api('/api/stations');
  for (const s of stations) {
    L.circleMarker([s.lat, s.lon], {
      renderer: state.renderer, radius: 3, color: css('--series-3'),
      weight: 1, fillColor: css('--series-3'), fillOpacity: 0.35,
    }).bindTooltip('Parking bay', { direction: 'top' }).addTo(layer);
  }
}

/* Rental hotspots. Bubble area is proportional to count, so the radius uses a
   square root — area, not radius, is what the eye reads as magnitude. */
function hotspotLayer(points, color, noun, layer) {
  if (!points.length) {
    toast(`No ${noun}s recorded yet — the collector needs more time`);
    return;
  }
  const max = Math.max(...points.map(p => p.n));
  for (const p of points) {
    L.circleMarker([p.lat, p.lon], {
      renderer: state.renderer,
      radius: 5 + 20 * Math.sqrt(p.n / max),
      color: 'transparent',
      fillColor: color,
      fillOpacity: 0.18 + 0.38 * (p.n / max),
    }).bindTooltip(`<b>${p.n}</b> ${noun}${p.n === 1 ? '' : 's'}`, { direction: 'top' })
      .addTo(layer);
  }
}

async function loadPickups(layer) {
  const { pickups } = await api(`/api/hotspots?hours=${state.windowHours}`);
  hotspotLayer(pickups, css('--series-1'), 'rental', layer);
}

async function loadDropoffs(layer) {
  const { dropoffs } = await api(`/api/hotspots?hours=${state.windowHours}`);
  hotspotLayer(dropoffs, css('--series-2'), 'drop-off', layer);
}

/* Net gain vs drain per area — a diverging encoding, so two hues with a
   neutral middle and area proportional to the size of the imbalance. */
async function loadBalance(layer) {
  const { cells } = await api(`/api/balance?hours=${state.windowHours}`);
  const live = cells.filter(c => c.net !== 0);
  if (!live.length) { toast('Not enough movement recorded yet for a balance map'); return; }
  const max = Math.max(...live.map(c => Math.abs(c.net)));
  for (const c of live) {
    const gain = c.net > 0;
    L.circleMarker([c.lat, c.lon], {
      renderer: state.renderer,
      radius: 5 + 18 * Math.sqrt(Math.abs(c.net) / max),
      color: 'transparent',
      fillColor: gain ? css('--series-3') : css('--series-2'),
      fillOpacity: 0.2 + 0.4 * (Math.abs(c.net) / max),
    }).bindTooltip(
      `<b>${gain ? '+' : ''}${c.net}</b> net ${gain ? 'gained' : 'drained'}` +
      `<span class="t-sub">${c.in} ended here · ${c.out} started here</span>`,
      { direction: 'top' }).addTo(layer);
  }
}

/* ── charts (hand-rolled SVG) ───────────────────────────────────────── */

const tip = (() => {
  const el = document.createElement('div');
  el.className = 'viz-tip';
  document.body.appendChild(el);
  return {
    show(html, x, y) {
      el.innerHTML = html;
      el.classList.add('show');
      const r = el.getBoundingClientRect();
      el.style.left = Math.min(x + 12, window.innerWidth - r.width - 8) + 'px';
      el.style.top = Math.max(8, y - r.height - 10) + 'px';
    },
    hide() { el.classList.remove('show'); },
  };
})();

const svgEl = (n, attrs = {}) => {
  const e = document.createElementNS('http://www.w3.org/2000/svg', n);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  return e;
};

function emptyChart(el, msg) {
  el.innerHTML = `<div class="empty">${msg}</div>`;
}

/* Vertical bars. Data ends are rounded 4px and anchored to the baseline. */
function barChart(el, data, opts = {}) {
  el.innerHTML = '';
  if (!data.length || data.every(d => !d.value)) {
    return emptyChart(el, opts.empty || 'Nothing recorded in this window yet.');
  }
  const W = Math.max(el.clientWidth || 380, 300), H = opts.height || 190;
  const pad = { t: 12, r: 10, b: 30, l: 40 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const max = Math.max(...data.map(d => d.value)) || 1;
  const gap = 2;                                  // 2px surface gap between bars
  // Cap the width so a two-bar chart does not stretch into slabs.
  const bw = Math.min(opts.maxBar || 72, Math.max(1, iw / data.length - gap));
  const step = bw + gap;
  const x0 = pad.l + (iw - (step * data.length - gap)) / 2;

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H });

  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih * (i / 4);
    svg.appendChild(svgEl('line', { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: 'gridline' }));
    const lab = svgEl('text', { x: pad.l - 6, y: y + 3, class: 'axis-label', 'text-anchor': 'end' });
    lab.textContent = fmtNum(Math.round(max * (1 - i / 4)));
    svg.appendChild(lab);
  }

  data.forEach((d, i) => {
    const h = Math.max(d.value > 0 ? 2 : 0, (d.value / max) * ih);
    const x = x0 + i * step, y = pad.t + ih - h;
    const rect = svgEl('rect', {
      x, y, width: bw, height: h, rx: Math.min(4, bw / 2),
      fill: d.color || css('--series-1'),
    });
    rect.addEventListener('mousemove', ev =>
      tip.show(`<b>${fmtNum(d.value)}</b> ${opts.unit || ''}<span class="t-sub">${d.label}</span>`,
               ev.clientX, ev.clientY));
    rect.addEventListener('mouseleave', tip.hide);
    svg.appendChild(rect);

    if (data.length <= 12 || i % Math.ceil(data.length / 8) === 0) {
      const t = svgEl('text', {
        x: x + bw / 2, y: H - 10, class: 'axis-label', 'text-anchor': 'middle',
      });
      t.textContent = d.short || d.label;
      svg.appendChild(t);
    }
  });

  el.appendChild(svg);
}

/* Multi-series line chart with a shared crosshair. */
function lineChart(el, series, opts = {}) {
  el.innerHTML = '';
  const any = series.some(s => s.points.length > 1);
  if (!any) return emptyChart(el, opts.empty || 'Not enough samples yet — this fills in as the collector runs.');

  const W = Math.max(el.clientWidth || 380, 300), H = opts.height || 190;
  const pad = { t: 12, r: 10, b: 26, l: 40 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

  const all = series.flatMap(s => s.points);
  const xs = all.map(p => p[0]), ys = all.map(p => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y1 = Math.max(...ys) * 1.08 || 1;
  const y0 = opts.zeroBased === false ? Math.min(...ys) * 0.92 : 0;

  const sx = t => pad.l + (x1 === x0 ? iw / 2 : ((t - x0) / (x1 - x0)) * iw);
  const sy = v => pad.t + ih - ((v - y0) / (y1 - y0 || 1)) * ih;

  const svg = svgEl('svg', { viewBox: `0 0 ${W} ${H}`, width: '100%', height: H });

  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih * (i / 4);
    svg.appendChild(svgEl('line', { x1: pad.l, x2: W - pad.r, y1: y, y2: y, class: 'gridline' }));
    const lab = svgEl('text', { x: pad.l - 6, y: y + 3, class: 'axis-label', 'text-anchor': 'end' });
    lab.textContent = fmtNum(Math.round(y0 + (y1 - y0) * (1 - i / 4)));
    svg.appendChild(lab);
  }

  [x0, x1].forEach((t, i) => {
    const lab = svgEl('text', {
      x: i ? W - pad.r : pad.l, y: H - 8, class: 'axis-label',
      'text-anchor': i ? 'end' : 'start',
    });
    lab.textContent = fmtTime(t);
    svg.appendChild(lab);
  });

  series.forEach(s => {
    const d = s.points.map((p, i) => `${i ? 'L' : 'M'}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join('');
    svg.appendChild(svgEl('path', {
      d, fill: 'none', stroke: s.color, 'stroke-width': 2,
      'stroke-linejoin': 'round', 'stroke-linecap': 'round',
    }));
  });

  const cross = svgEl('line', {
    y1: pad.t, y2: pad.t + ih, class: 'gridline', stroke: css('--text-muted'), opacity: 0,
  });
  svg.appendChild(cross);
  const dots = series.map(s => {
    const c = svgEl('circle', { r: 4, fill: s.color, stroke: css('--surface-1'), 'stroke-width': 2, opacity: 0 });
    svg.appendChild(c);
    return c;
  });

  const hit = svgEl('rect', { x: pad.l, y: pad.t, width: iw, height: ih, fill: 'transparent' });
  hit.addEventListener('mousemove', ev => {
    const box = svg.getBoundingClientRect();
    const px = ((ev.clientX - box.left) / box.width) * W;
    const t = x0 + ((px - pad.l) / iw) * (x1 - x0);
    let rows = '', shownT = null;
    series.forEach((s, i) => {
      if (!s.points.length) { dots[i].setAttribute('opacity', 0); return; }
      let best = s.points[0];
      for (const p of s.points) if (Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      shownT = best[0];
      dots[i].setAttribute('cx', sx(best[0]));
      dots[i].setAttribute('cy', sy(best[1]));
      dots[i].setAttribute('opacity', 1);
      rows += `<div><b>${fmtNum(best[1])}</b> ${s.name}</div>`;
    });
    cross.setAttribute('x1', sx(shownT)); cross.setAttribute('x2', sx(shownT));
    cross.setAttribute('opacity', 0.5);
    tip.show(rows + `<span class="t-sub">${fmtTime(shownT)}</span>`, ev.clientX, ev.clientY);
  });
  hit.addEventListener('mouseleave', () => {
    tip.hide(); cross.setAttribute('opacity', 0);
    dots.forEach(d => d.setAttribute('opacity', 0));
  });
  svg.appendChild(hit);
  el.appendChild(svg);

  if (series.length > 1) {
    const leg = document.createElement('div');
    leg.className = 'chart-legend';
    leg.innerHTML = series.map(s =>
      `<span><i style="background:${s.color}"></i>${s.name}</span>`).join('');
    el.appendChild(leg);
  }
}

/* ── analytics tab ──────────────────────────────────────────────────── */

async function loadStats() {
  let s;
  try {
    s = await api('/api/stats?hours=' + state.windowHours);
  } catch (e) { toast('Stats failed: ' + e.message); return; }
  state.stats = s;
  renderStats(s);
}

function renderStats(s) {
  const f = s.fleet, a = s.activity, c = s.charts;
  const obs = s.coverage.observing_s;

  $('#windowNote').textContent =
    `Last ${s.window_hours}h · ${fmtNum(s.coverage.polls)} polls over ${fmtDur(obs)} of observation`;

  const ride = a.est_ride_min;
  const needH = a.needs_hours_for_estimate;
  const tiles = [
    ['Available now', fmtNum(f.available), `${fmtNum(f.scooters)} scooters · ${fmtNum(f.bicycles)} bikes`],
    ['Est. out riding', fmtNum(f.riding), `${f.utilisation_pct}% of ${fmtNum(f.peak_available)} deployed`],
    ['Rentals started', fmtNum(a.rentals_started), `${fmtNum(a.per_hour, 1)} per hour`],
    ['Rentals ended', fmtNum(a.rentals_ended), 'new IDs appearing'],
    ['Rentals per vehicle', fmtNum(a.per_vehicle_per_day, 2), 'per day, at this rate'],
    ['Est. ride length', ride ? ride + ' min' : '—',
      ride ? "Little's Law estimate" : `needs ${fmtNum(needH, 1)}h more data`],
    ['Median wait', a.median_dwell_s != null ? fmtDur(a.median_dwell_s) : '—', 'parked before being taken'],
    ['Avg battery', f.avg_fuel_pct + '%', `${fmtNum(f.low_battery)} under 20%`],
  ];
  $('#tiles').innerHTML = tiles.map(([k, v, sub]) =>
    `<div class="tile"><div class="tile-k">${k}</div><div class="tile-v">${v}</div>
     <div class="tile-sub">${sub}</div></div>`).join('');

  /* fleet on the street */
  const samples = c.fleet_samples;
  /* Scooters and bikes share one axis because they are the same measure at a
     comparable scale. Rides-in-progress is two orders of magnitude smaller, so
     it gets its own chart rather than a second y-axis. */
  lineChart($('#chartAvail'), [
    { name: 'Scooters', color: css('--series-1'), points: samples.map(r => [r.ts, r.scooters]) },
    { name: 'Bikes',    color: css('--series-2'), points: samples.map(r => [r.ts, r.bicycles]) },
  ], { zeroBased: false, empty: 'Needs at least two polls — check back in a minute.' });

  lineChart($('#chartRiding'), [
    { name: 'Rides in progress', color: css('--series-3'), points: samples.map(r => [r.ts, r.riding]) },
  ], { empty: 'Needs at least two polls — check back in a minute.' });

  /* rentals per hour, gap-filled, starts vs ends */
  const hourly = c.hourly;
  let starts = [], ends = [];
  if (hourly.length) {
    const first = hourly[0].ts, last = hourly[hourly.length - 1].ts;
    const byTs = Object.fromEntries(hourly.map(h => [h.ts, h]));
    for (let t = first; t <= last; t += 3600) {
      starts.push([t, byTs[t]?.pickups || 0]);
      ends.push([t, byTs[t]?.dropoffs || 0]);
    }
  }
  if (starts.length > 1) {
    lineChart($('#chartHourly'), [
      { name: 'Rentals started', color: css('--series-1'), points: starts },
      { name: 'Rentals ended',   color: css('--series-2'), points: ends },
    ], {});
  } else {
    /* Under an hour of data a line has nothing to say; show the totals instead. */
    barChart($('#chartHourly'), [
      { label: 'Rentals started', short: 'Started', value: a.rentals_started, color: css('--series-1') },
      { label: 'Rentals ended', short: 'Ended', value: a.rentals_ended, color: css('--series-2') },
    ], {
      unit: 'so far', maxBar: 90,
      empty: 'No rentals observed yet. The first ones appear within a couple of minutes of the collector starting.',
    });
  }

  const bandColors = [css('--bat-low'), css('--bat-low'), css('--bat-mid'), css('--bat-mid'),
                      css('--bat-mid'), css('--bat-high'), css('--bat-high'), css('--bat-high'),
                      css('--bat-high'), css('--bat-high')];
  barChart($('#chartBattery'),
    ['0–10', '10–20', '20–30', '30–40', '40–50', '50–60', '60–70', '70–80', '80–90', '90–100']
      .map((l, i) => ({ label: l + '%', short: l.split('–')[0], value: c.battery_hist[i], color: bandColors[i] })),
    { unit: 'vehicles' });

  barChart($('#chartIdle'),
    ['<1h', '1–3h', '3–6h', '6–12h', '12–24h', '24–48h', '48–72h', '72h+']
      .map((l, i) => ({ label: l, short: l, value: c.idle_hist[i] })),
    { unit: 'vehicles' });

  barChart($('#chartHourOfDay'),
    (c.hour_of_day || []).map((v, i) => ({
      label: pad(i) + ':00', short: i % 3 === 0 ? pad(i) : '', value: v,
    })),
    { unit: 'rentals/hour', empty: 'Fills in as the collector spans more of the day.' });

  barChart($('#chartCentre'),
    ['<1km', '1–2km', '2–3km', '3–5km', '5–8km', '8–12km', '12km+']
      .map((l, i) => ({ label: l, short: l.replace('–', '-'), value: (c.centre_hist || [])[i] })),
    { unit: 'vehicles' });

  renderTransit(s.transit || {});
  renderRail(s.rail || {});

  barChart($('#chartDwell'),
    ['<15m', '15–30m', '30–60m', '1–2h', '2–4h', '4–8h', '8–24h', '24h+']
      .map((l, i) => ({ label: l, short: l.replace('–', '-'), value: c.dwell_hist[i] })),
    { unit: 'rentals', empty: 'No rentals observed yet — this fills in as vehicles get taken.' });

  /* Short axis labels; the full wording stays in the tooltip. */
  const kindOrder = ['relocation', 'service', 'trip'];
  const kindShort = { relocation: 'Van', service: 'Battery', trip: 'Other' };
  const kindCols = [css('--series-2'), css('--series-3'), css('--series-1')];
  barChart($('#chartKinds'),
    kindOrder.map((k, i) => ({
      label: KIND_LABEL[k], short: kindShort[k], value: a.ops_moves[k] || 0, color: kindCols[i],
    })),
    { unit: 'moves', height: 170,
      empty: 'No operational moves seen yet. These are rarer than rentals — vans rebalancing or swapping batteries.' });
}

function renderTransit(t) {
  $('#transitNote').textContent = t.live != null
    ? `${fmtNum(t.live)} vehicles reporting live · ${fmtNum(t.routes)} routes · ${fmtNum(t.stations)} rail stations mapped`
    : 'no transit data yet';

  $('#transitTiles').innerHTML = [
    ['Buses live', fmtNum(t.live || 0), 'fresh position in last 15 min'],
    ['Actually moving', fmtNum(t.moving || 0), 'above 3 km/h'],
    ['Routes running', fmtNum(t.routes || 0), 'distinct GTFS route IDs'],
    ['Operators', fmtNum(t.operators || 0), 'reporting in Bristol'],
    ['Avg bus speed', t.avg_speed_kmh != null ? t.avg_speed_kmh + ' km/h' : '—', 'moving vehicles only'],
    ['Rail stations', fmtNum(t.stations || 0), 'positions only — no live trains'],
  ].map(([k, v, sub]) =>
    `<div class="tile"><div class="tile-k">${k}</div><div class="tile-v">${v}</div>
     <div class="tile-sub">${sub}</div></div>`).join('');

  const samples = t.samples || [];
  lineChart($('#chartBuses'), [
    { name: 'Reporting', color: css('--series-3'), points: samples.map(r => [r.ts, r.active]) },
    { name: 'Moving', color: css('--series-2'), points: samples.filter(r => r.moving != null).map(r => [r.ts, r.moving]) },
  ], { empty: 'Needs at least two transit polls — these run every couple of minutes.' });

  barChart($('#chartOperators'),
    (t.by_operator || []).slice(0, 6).map(o => ({
      label: o.operator, short: o.operator.split(' ')[0], value: o.n, color: css('--series-3'),
    })),
    { unit: 'buses', empty: 'No operators reporting yet.' });

  barChart($('#chartBusSpeed'),
    ['0–5', '5–10', '10–15', '15–20', '20–25', '25–30', '30–40', '40+']
      .map((l, i) => ({ label: l + ' km/h', short: l.split('–')[0], value: (t.speed_hist || [])[i] })),
    { unit: 'fixes', empty: 'Speed needs two consecutive fixes per vehicle — building.' });
}

function renderRail(r) {
  if (!r.enabled) {
    $('#railNote').textContent = 'not configured';
    $('#railTiles').innerHTML =
      `<div class="empty" style="grid-column:1/-1">Rail is off. Set <code>HUSH_RTT_TOKEN</code>
       to a Realtime Trains token and restart to switch it on.</div>`;
    ['#chartRailDelay', '#chartRailOperators'].forEach(id => emptyChart($(id), 'Rail not configured.'));
    $('#railWorst').innerHTML = '';
    return;
  }

  $('#railNote').textContent =
    `${fmtNum(r.services)} services seen across ${fmtNum(r.stations)} stations`;

  $('#railTiles').innerHTML = [
    ['Services tracked', fmtNum(r.services), `across ${fmtNum(r.stations)} stations`],
    ['Average delay', r.mean_delay_min != null ? (r.mean_delay_min > 0 ? '+' : '') + r.mean_delay_min + ' min' : '—', 'excludes cancellations'],
    ['On time', r.on_time_pct != null ? r.on_time_pct + '%' : '—', 'within 5 minutes'],
    ['Cancelled', fmtNum(r.cancelled || 0), 'in this window'],
  ].map(([k, v, sub]) =>
    `<div class="tile"><div class="tile-k">${k}</div><div class="tile-v">${v}</div>
     <div class="tile-sub">${sub}</div></div>`).join('');

  /* Delay is a diverging measure — early, on time, late — so early running
     gets its own colour rather than being lumped in with lateness. */
  const early = css('--series-1'), ok = css('--series-3'), late = css('--bat-low');
  barChart($('#chartRailDelay'),
    [['Early', early], ['On time', ok], ['≤2 min', ok], ['3–5 min', ok],
     ['6–10 min', css('--bat-mid')], ['11–30 min', late], ['30 min+', late]]
      .map(([l, colour], i) => ({
        label: l, short: l.replace(' min', ''), value: (r.delay_hist || [])[i], color: colour,
      })),
    { unit: 'services', empty: 'No rail services observed yet.' });

  barChart($('#chartRailOperators'),
    (r.by_operator || []).filter(o => o.avg_delay != null).slice(0, 6).map(o => ({
      label: `${o.operator} · ${o.n} services`,
      short: o.operator.replace('Great Western Railway', 'GWR').split(' ')[0],
      value: o.avg_delay,
      color: o.avg_delay > 5 ? css('--bat-low') : css('--series-3'),
    })),
    { unit: 'min average delay', empty: 'No operator data yet.' });

  const worst = r.worst || [];
  $('#railWorst').innerHTML = worst.length
    ? `<table><thead><tr><th>Service</th><th>Operator</th><th>Towards</th><th>At</th><th>Delay</th></tr></thead><tbody>` +
      worst.map(w => `<tr>
          <td class="id">${w.headcode || '—'}</td>
          <td>${w.operator || '—'}</td>
          <td>${w.destination || '—'}</td>
          <td class="id">${w.station_code}</td>
          <td class="num" style="color:${w.delay_min > 5 ? css('--bat-low') : 'inherit'}">
            ${w.delay_min > 0 ? '+' : ''}${w.delay_min} min</td>
        </tr>`).join('') + `</tbody></table>`
    : `<div class="empty" style="margin:16px">Nothing delayed right now.</div>`;
}

/* ── fleet table ────────────────────────────────────────────────────── */

async function loadFleet() {
  let d;
  try { d = await api('/api/leaderboard?hours=' + state.windowHours); }
  catch (e) { toast('Fleet failed: ' + e.message); return; }

  const typeName = t => (t === 'dott_bicycle' ? 'Bike' : 'Scooter');
  const rowsFor = view => {
    if (view === 'quickest') {
      if (!d.quickest.length) return null;
      return {
        head: ['Vehicle', 'Type', 'Waited', 'Battery', 'Taken at', 'Location'],
        rows: d.quickest.map(r => [
          r.bike_id, typeName(r.vehicle_type_id), fmtDur(r.dwell_s),
          Math.round((r.fuel || 0) * 100) + '%', fmtTime(r.ts),
          `${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}`,
        ]),
        note: 'Vehicles rented soonest after being parked — these spots are in demand.',
      };
    }
    const src = view === 'flat' ? d.flat : d.stranded;
    if (!src.length) return null;
    return {
      head: ['Vehicle', 'Type', 'Idle for', 'Battery', 'Location'],
      rows: src.map(r => [
        r.bike_id, typeName(r.vehicle_type_id), fmtDur(r.idle_s),
        Math.round((r.fuel || 0) * 100) + '%',
        `${r.lat.toFixed(4)}, ${r.lon.toFixed(4)}`,
      ]),
      note: view === 'flat'
        ? 'Lowest battery on the street — the recharge collection list.'
        : 'Parked longest without being touched — dead stock worth relocating.',
    };
  };

  const t = rowsFor(state.fleetView);
  const el = $('#fleetTable');
  if (!t) {
    el.innerHTML = `<div class="empty" style="margin:20px">
      Nothing recorded in this window yet — the collector builds this as it watches the feed.</div>`;
    return;
  }
  el.innerHTML =
    `<p class="table-note">${t.note}</p>` +
    `<table><thead><tr>${t.head.map(h => `<th>${h}</th>`).join('')}</tr></thead><tbody>` +
    t.rows.map(r =>
      `<tr data-id="${r[0]}">` +
      r.map((c, i) => `<td class="${i === 0 ? 'id' : i > 1 ? 'num' : ''}">${i === 0 ? c.slice(0, 8) + '…' : c}</td>`).join('') +
      `</tr>`).join('') +
    `</tbody></table>`;

  $$('#fleetTable tr[data-id]').forEach(tr => {
    tr.addEventListener('click', () => {
      switchTab('map');
      selectVehicle(tr.dataset.id);
    });
  });
}

/* ── tabs, theme, wiring ────────────────────────────────────────────── */

function switchTab(name) {
  $$('.tab').forEach(t => {
    const on = t.dataset.tab === name;
    t.classList.toggle('is-active', on);
    t.setAttribute('aria-selected', String(on));
  });
  $$('.panel').forEach(p => p.classList.toggle('is-active', p.id === 'tab-' + name));
  if (name === 'map' && state.map) setTimeout(() => state.map.invalidateSize(), 60);
  if (name === 'analytics') loadStats();
  if (name === 'fleet') loadFleet();
}

function segment(sel, onPick) {
  $$(sel + ' button').forEach(b => {
    b.addEventListener('click', () => {
      $$(sel + ' button').forEach(x => x.classList.remove('is-active'));
      b.classList.add('is-active');
      onPick(b.dataset.v);
    });
  });
}

function wire() {
  $$('.tab').forEach(t => t.addEventListener('click', () => switchTab(t.dataset.tab)));

  segment('#colorBy', v => { state.colorBy = v; renderLegend(); drawVehicles(); });
  segment('#typeFilter', v => { state.filters.type = v; loadLive(); });
  segment('#windowSel', v => { state.windowHours = +v; loadStats(); loadFleet(); });
  segment('#fleetSel', v => { state.fleetView = v; loadFleet(); });

  const fuel = $('#fuelRange');
  fuel.addEventListener('input', () => { $('#fuelVal').textContent = fuel.value + '%'; });
  fuel.addEventListener('change', () => { state.filters.minFuel = +fuel.value; loadLive(); });

  $('#layerBuses').addEventListener('change', e => {
    if (e.target.checked) { state.layers.buses.addTo(state.map); loadBuses(); }
    else state.map.removeLayer(state.layers.buses);
  });
  $('#layerScooters').addEventListener('change', e => {
    if (e.target.checked) state.layers.vehicles.addTo(state.map);
    else state.map.removeLayer(state.layers.vehicles);
  });
  $('#layerTrains').addEventListener('change', e => {
    if (e.target.checked) { state.layers.trains.addTo(state.map); loadTrains(); }
    else state.map.removeLayer(state.layers.trains);
  });
  $('#layerRail').addEventListener('change', e => toggleLayer('rail', e.target.checked, loadRail));
  $('#layerStops').addEventListener('change', e => toggleLayer('stops', e.target.checked,
    l => loadInfrastructure(['bus_stop', 'bus_station'], l, 'bus stops')));
  $('#layerFerry').addEventListener('change', e => toggleLayer('ferry', e.target.checked,
    l => loadInfrastructure(['ferry_terminal'], l, 'ferry piers')));

  $('#layerZones').addEventListener('change', e => toggleLayer('zones', e.target.checked, loadZones));
  $('#layerStations').addEventListener('change', e => toggleLayer('stations', e.target.checked, loadStations));
  $('#layerPickups').addEventListener('change', e => toggleLayer('pickups', e.target.checked, loadPickups));
  $('#layerDropoffs').addEventListener('change', e => toggleLayer('dropoffs', e.target.checked, loadDropoffs));
  $('#layerBalance').addEventListener('change', e => toggleLayer('balance', e.target.checked, loadBalance));

  $('#detailClose').addEventListener('click', closeDetail);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDetail(); });

  $('#themeToggle').addEventListener('click', () => {
    const next = document.documentElement.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('hush-theme', next);
    setTiles();
    drawVehicles();
    renderLegend();
    if (state.stats) renderStats(state.stats);
  });

  let rt;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => { if (state.stats) renderStats(state.stats); }, 250);
  });
}

async function main() {
  const saved = localStorage.getItem('hush-theme');
  if (saved) document.documentElement.setAttribute('data-theme', saved);

  wire();
  renderLegend();

  const h = await loadHealth();
  initMap(h?.center || [51.4545, -2.5879], h?.zoom || 13);
  state.map.on('zoomend', () => drawVehicles());

  await loadLive();
  loadBuses();
  loadTrains();
  toggleLayer('rail', true, loadRail);

  if (h && h.polls <= 1) {
    toast('Collector just started — ride history builds up as it watches the feed', 5200);
  }

  setInterval(loadLive, 30000);
  setInterval(() => { if ($('#layerBuses').checked) loadBuses(); }, 45000);
  setInterval(() => { if ($('#layerTrains').checked) loadTrains(); }, 60000);
  setInterval(loadHealth, 15000);
  setInterval(() => {
    if ($('#tab-analytics').classList.contains('is-active')) loadStats();
  }, 60000);
}

main();
