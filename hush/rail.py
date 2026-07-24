"""National Rail services via the Realtime Trains API.

What this source does and does not give you, checked against the live API
rather than assumed:

  * It returns *timing*: which services call at a station, their scheduled and
    real-time forecast times, delays, cancellations, platforms and operators.
  * It does **not** return coordinates. Neither the service data nor the
    reference data (`/data/stops`, `/data/locations_ungrouped`) carries a
    latitude or longitude anywhere.

So trains cannot be drawn as moving dots from this feed, and Hush does not
pretend to. Instead each rail station becomes live: click it for a real
departure board, and the delay statistics are computed from real forecasts.
Station coordinates come from OpenStreetMap and are matched to RTT station
codes by name.

Authentication: the configured token is a long-life *refresh* token. It buys a
20-minute access token from /api/get_access_token, which is what actually
signs requests. RTT's terms require the token never reach an end-user
application, so it lives here in the server and is never sent to the browser.
"""

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, db
from .collector import haversine_m

BASE = "https://data.rtt.io"

_token_lock = threading.Lock()
_access = {"token": None, "expires": 0}


class RailUnavailable(Exception):
    """Raised when rail data cannot be fetched (no token, auth failure)."""


# RTT allows 30 requests a minute. Board polls and position lookups both draw
# on it, so the budget is tracked in one place and callers simply block until
# there is room, rather than each guessing its own sleep.
_RATE_WINDOW_S = 60
_RATE_MAX = 26                      # a little under the ceiling
_recent_calls = []
_rate_lock = threading.Lock()


def _await_slot():
    while True:
        with _rate_lock:
            now = time.time()
            _recent_calls[:] = [t for t in _recent_calls if now - t < _RATE_WINDOW_S]
            if len(_recent_calls) < _RATE_MAX:
                _recent_calls.append(now)
                return
            wait = _RATE_WINDOW_S - (now - _recent_calls[0]) + 0.2
        time.sleep(max(0.5, wait))


def _request(path, token, timeout=30):
    _await_slot()
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": "Bearer " + token,
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8")), resp.headers


def access_token():
    """Return a valid short-life access token, refreshing when it expires."""
    if not config.RTT_TOKEN:
        raise RailUnavailable("no RTT token configured (set HUSH_RTT_TOKEN)")
    with _token_lock:
        now = time.time()
        if _access["token"] and now < _access["expires"] - 60:
            return _access["token"]
        try:
            payload, _ = _request("/api/get_access_token", config.RTT_TOKEN)
        except urllib.error.HTTPError as exc:
            raise RailUnavailable(f"token refresh rejected ({exc.code})") from exc
        token = payload.get("token")
        if not token:
            raise RailUnavailable("token refresh returned no token")
        # Trust the token's own expiry rather than assuming 20 minutes.
        expires = now + 1100
        try:
            body = token.split(".")[1]
            body += "=" * (-len(body) % 4)
            import base64
            claims = json.loads(base64.urlsafe_b64decode(body))
            if claims.get("exp"):
                expires = claims["exp"]
        except Exception:
            pass
        _access.update(token=token, expires=expires)
        print("[rail] refreshed access token", flush=True)
        return token


# --- station matching ----------------------------------------------------------

def _norm(name):
    s = (name or "").lower().replace("&", "and").replace(".", "")
    s = re.sub(r"\b(railway|rail)?\s*station\b", "", s)
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def sync_stations():
    """Match OpenStreetMap rail stations to RTT station codes by name.

    Heritage and miniature railways (Avon Valley, the SS Great Britain line)
    are in OSM but not on the national network, so they simply do not match
    and are left out.
    """
    token = access_token()
    payload, _ = _request("/data/stops", token, timeout=60)
    by_name = {}
    for s in payload.get("stops", []):
        if s.get("shortCode"):
            by_name.setdefault(_norm(s.get("description")), s)

    # Stations are matched over a wide region, not just the map bounding box:
    # a train's calling points run to Cardiff, Taunton and Swindon, and every
    # one of them needs coordinates for the position estimate to work.
    osm = db.rows(
        "SELECT name, lat, lon FROM osm_features "
        "WHERE kind IN ('rail_station','rail_halt') AND name IS NOT NULL")
    lo_la, lo_lo, hi_la, hi_lo = config.RAIL_STATION_BBOX
    try:
        wide = _overpass(
            f"[out:json][timeout:120];node[railway=station]"
            f"({lo_la},{lo_lo},{hi_la},{hi_lo});out body;")
        for el in wide.get("elements", []):
            name = (el.get("tags") or {}).get("name")
            if name and el.get("lat") is not None:
                osm.append({"name": name, "lat": el["lat"], "lon": el["lon"]})
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"[rail] wide station fetch failed, using local only: {exc}", flush=True)
    conn = db.connect()
    rows = []
    for o in osm:
        match = by_name.get(_norm(o["name"]))
        if match:
            rows.append((match["shortCode"], match.get("description") or o["name"],
                         o["lat"], o["lon"], int(time.time())))
    if rows:
        conn.executemany(
            "INSERT INTO rail_stations(code, name, lat, lon, synced_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
            "name=excluded.name, lat=excluded.lat, lon=excluded.lon, "
            "synced_at=excluded.synced_at", rows)
        conn.commit()
    print(f"[rail] matched {len(rows)} stations to RTT codes", flush=True)
    return len(rows)


# --- live boards ---------------------------------------------------------------

def _parse_dt(value):
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return None


def _leg(service, key):
    """Pick arrival or departure timing, whichever the service has."""
    temporal = service.get("temporalData") or {}
    return temporal.get(key) or {}


def _describe(points):
    if not points:
        return None
    loc = (points[0] or {}).get("location") or {}
    return loc.get("description")


def poll_station(code, token, now):
    """Fetch one station's board and return rows ready for insert."""
    path = "/gb-nr/location?" + urllib.parse.urlencode({"code": code})
    payload, headers = _request(path, token)
    out = []
    for svc in payload.get("services", []):
        meta = svc.get("scheduleMetadata") or {}
        if not meta.get("inPassengerService", True):
            continue
        temporal = svc.get("temporalData") or {}
        arr, dep = temporal.get("arrival") or {}, temporal.get("departure") or {}
        # A service either terminates here (arrival only) or passes through.
        leg = dep or arr
        sched = _parse_dt(leg.get("scheduleAdvertised") or leg.get("scheduleInternal"))
        fore = _parse_dt(leg.get("realtimeForecast"))
        cancelled = 1 if (leg.get("isCancelled") or arr.get("isCancelled")) else 0
        delay = None
        if sched and fore:
            delay = int(round((fore - sched) / 60.0))
        operator = (meta.get("operator") or {})
        platform = ((svc.get("locationMetadata") or {}).get("platform") or {})
        out.append((
            meta.get("uniqueIdentity") or f"{code}:{meta.get('identity')}",
            code,
            meta.get("trainReportingIdentity"),
            operator.get("code"), operator.get("name"),
            _describe(svc.get("origin")), _describe(svc.get("destination")),
            sched, fore, delay, cancelled,
            platform.get("forecast") or platform.get("planned"),
            (svc.get("locationMetadata") or {}).get("numberOfVehicles"),
            "departure" if dep else "arrival",
            now,
        ))
    return out, headers


def poll():
    """Poll every matched station's live board."""
    try:
        token = access_token()
    except RailUnavailable as exc:
        print(f"[rail] skipped: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}

    # Boards are only polled for stations on the map. The table also holds
    # several hundred stations across the region, but those are there to give
    # calling points coordinates, not to be polled -- doing so would burn the
    # per-minute allowance and starve everything else.
    lo_la, lo_lo, hi_la, hi_lo = config.BBOX
    stations = db.rows(
        "SELECT code FROM rail_stations WHERE lat BETWEEN ? AND ? "
        "AND lon BETWEEN ? AND ? ORDER BY code",
        (lo_la, hi_la, lo_lo, hi_lo))
    if not stations:
        try:
            sync_stations()
        except (RailUnavailable, urllib.error.URLError, OSError, ValueError) as exc:
            print(f"[rail] station sync failed: {exc}", flush=True)
            return {"ok": False, "error": str(exc)}
        stations = db.rows("SELECT code FROM rail_stations ORDER BY code")

    now = int(time.time())
    conn = db.connect()
    rows, polled, failed = [], 0, 0
    remaining = None

    for st in stations:
        try:
            got, headers = poll_station(st["code"], token, now)
        except urllib.error.HTTPError as exc:
            failed += 1
            if exc.code == 429:
                print("[rail] rate limited; stopping this cycle", flush=True)
                break
            continue
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            failed += 1
            continue
        rows.extend(got)
        polled += 1
        remaining = headers.get("X-RateLimit-Remaining-Hour", remaining)

    if rows:
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "INSERT INTO rail_services(uid, station_code, headcode, operator_code, "
                "operator, origin, destination, scheduled_ts, forecast_ts, delay_min, "
                "is_cancelled, platform, coaches, leg, seen_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(uid, station_code) DO UPDATE SET "
                " forecast_ts=excluded.forecast_ts, delay_min=excluded.delay_min,"
                " is_cancelled=excluded.is_cancelled, platform=excluded.platform,"
                " seen_ts=excluded.seen_ts", rows)
            delays = [r[9] for r in rows if r[9] is not None and not r[10]]
            conn.execute(
                "INSERT OR REPLACE INTO rail_samples(ts, stations, services, "
                "cancelled, mean_delay_min, on_time_pct) VALUES(?,?,?,?,?,?)",
                (now, polled, len(rows), sum(r[10] for r in rows),
                 (sum(delays) / len(delays)) if delays else None,
                 (100.0 * sum(1 for d in delays if d <= 5) / len(delays)) if delays else None))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    print(f"[rail] {time.strftime('%H:%M:%S')} {polled} stations, {len(rows)} services"
          + (f", {failed} failed" if failed else "")
          + (f", {remaining} req left this hour" if remaining else ""), flush=True)
    return {"ok": True, "stations": polled, "services": len(rows), "failed": failed}


def prune():
    conn = db.connect()
    conn.execute("DELETE FROM rail_services WHERE seen_ts < ?",
                 (int(time.time()) - config.RAIL_RETAIN_S,))
    conn.commit()


# --- estimated train positions -------------------------------------------------
#
# RTT gives no coordinates, so a train's position is reconstructed from its
# calling pattern:
#
#   1. take the service's ordered calling points and their times, preferring
#      the actual time a train really passed over a forecast;
#   2. find which leg "now" falls in -- either dwelling at a station, or
#      running between two of them;
#   3. interpolate along that leg by elapsed time;
#   4. snap the result onto real OpenStreetMap track geometry so the train sits
#      on a railway rather than cutting across country.
#
# The result is an estimate and is labelled as one everywhere it is shown. It
# is only as good as the timings: a train that is between two widely spaced
# calling points, or running to a stale forecast, will drift.

def _overpass(query, timeout=180):
    from .transit import _fetch
    url = config.OVERPASS_URL + "?data=" + urllib.parse.quote(query, safe="")
    raw = _fetch(url, timeout=timeout, headers={
        "Accept": "application/json", "User-Agent": config.USER_AGENT})
    return json.loads(raw.decode("utf-8"))


def refresh_track(force=False):
    """Load railway line geometry from OpenStreetMap.

    Overpass rejects `out geom` through some proxies, so ways and nodes are
    fetched separately and stitched together here.
    """
    now = int(time.time())
    have = db.scalar("SELECT COUNT(*) FROM rail_track", default=0)
    newest = db.scalar("SELECT MAX(fetched_at) FROM rail_track", default=0) or 0
    if have and not force and now - newest < config.OSM_REFRESH_S:
        return 0

    lo_la, lo_lo, hi_la, hi_lo = config.RAIL_TRACK_BBOX
    bbox = f"({lo_la},{lo_lo},{hi_la},{hi_lo})"
    try:
        ways = _overpass(f"[out:json][timeout:120];way[railway=rail]{bbox};out body;")
        time.sleep(8)
        nodes = _overpass(f"[out:json][timeout:120];way[railway=rail]{bbox};node(w);out skel;")
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"[rail] track geometry fetch failed: {exc}", flush=True)
        return 0

    coords = {n["id"]: (n["lat"], n["lon"]) for n in nodes.get("elements", [])
              if "lat" in n}
    rows = []
    for way in ways.get("elements", []):
        refs = way.get("nodes") or []
        for a, b in zip(refs, refs[1:]):
            pa, pb = coords.get(a), coords.get(b)
            if not pa or not pb:
                continue
            rows.append((pa[0], pa[1], pb[0], pb[1],
                         min(pa[0], pb[0]), min(pa[1], pb[1]),
                         max(pa[0], pb[0]), max(pa[1], pb[1]), now))
    if not rows:
        return 0
    conn = db.connect()
    conn.execute("DELETE FROM rail_track")
    conn.executemany(
        "INSERT INTO rail_track(lat1, lon1, lat2, lon2, min_lat, min_lon, "
        "max_lat, max_lon, fetched_at) VALUES(?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    print(f"[rail] track geometry: {len(rows)} segments", flush=True)
    return len(rows)


_track_cache = {"loaded_at": 0, "segments": []}


def _track():
    """Segments held in memory; reloaded occasionally."""
    now = time.time()
    if _track_cache["segments"] and now - _track_cache["loaded_at"] < 3600:
        return _track_cache["segments"]
    segs = [(r["lat1"], r["lon1"], r["lat2"], r["lon2"])
            for r in db.rows("SELECT lat1, lon1, lat2, lon2 FROM rail_track")]
    _track_cache.update(loaded_at=now, segments=segs)
    return segs


def _snap(lat, lon):
    """Move a point onto the nearest railway segment.

    Returns (lat, lon, metres_moved). Far-away matches are rejected by the
    caller: if the nearest track is hundreds of metres off it is probably a
    different line, and the interpolated point is the better answer.
    """
    segs = _track()
    if not segs:
        return lat, lon, None
    # Degrees-to-metres is latitude dependent; at 51.5N one degree of longitude
    # is about 0.62 of a degree of latitude. Work in a locally flat frame.
    kx = math.cos(math.radians(lat))
    best = None
    for y1, x1, y2, x2 in segs:
        if abs(y1 - lat) > 0.02 and abs(y2 - lat) > 0.02:
            continue                       # cheap reject before the maths
        dy, dx = y2 - y1, (x2 - x1) * kx
        py, px = lat - y1, (lon - x1) * kx
        denom = dy * dy + dx * dx
        t = 0.0 if denom == 0 else max(0.0, min(1.0, (py * dy + px * dx) / denom))
        cy, cx = y1 + t * dy, x1 + t * (x2 - x1)
        d2 = (lat - cy) ** 2 + ((lon - cx) * kx) ** 2
        if best is None or d2 < best[0]:
            best = (d2, cy, cx)
    if best is None:
        return lat, lon, None
    moved = haversine_m(lat, lon, best[1], best[2])
    return best[1], best[2], moved


_graph_cache = {"loaded_at": 0, "adj": None, "nodes": None}
_route_cache = {}


def _graph():
    """Undirected graph of the railway, nodes keyed by rounded coordinate.

    Rounding to 5dp (about a metre) rejoins ways that share an endpoint, which
    is what makes the separate way/node fetches usable as a network.
    """
    now = time.time()
    if _graph_cache["adj"] is not None and now - _graph_cache["loaded_at"] < 3600:
        return _graph_cache["adj"], _graph_cache["nodes"]
    adj = {}
    for lat1, lon1, lat2, lon2 in _track():
        a = (round(lat1, 5), round(lon1, 5))
        b = (round(lat2, 5), round(lon2, 5))
        if a == b:
            continue
        d = haversine_m(lat1, lon1, lat2, lon2)
        adj.setdefault(a, []).append((b, d))
        adj.setdefault(b, []).append((a, d))
    nodes = list(adj)
    _graph_cache.update(loaded_at=now, adj=adj, nodes=nodes)
    return adj, nodes


def _nearest_node(nodes, lat, lon, max_m=2000):
    kx = math.cos(math.radians(lat))
    best, best_d2 = None, None
    for n in nodes:
        dy = n[0] - lat
        if abs(dy) > 0.03:
            continue
        dx = (n[1] - lon) * kx
        d2 = dy * dy + dx * dx
        if best_d2 is None or d2 < best_d2:
            best, best_d2 = n, d2
    if best is None:
        return None
    return best if haversine_m(lat, lon, best[0], best[1]) <= max_m else None


def _route(a, b, limit_nodes=200000):
    """Shortest path along the track between two graph nodes.

    Returns [(lat, lon, cumulative_metres), ...] or None. Dijkstra, with an
    expansion cap so a disconnected or distant pair fails fast rather than
    walking the whole network.
    """
    import heapq
    adj, _ = _graph()
    if a not in adj or b not in adj:
        return None
    dist = {a: 0.0}
    prev = {}
    heap = [(0.0, a)]
    seen = set()
    while heap:
        d, node = heapq.heappop(heap)
        if node in seen:
            continue
        seen.add(node)
        if node == b:
            break
        if len(seen) > limit_nodes:
            return None
        for nxt, w in adj.get(node, ()):
            nd = d + w
            if nd < dist.get(nxt, float("inf")):
                dist[nxt] = nd
                prev[nxt] = node
                heapq.heappush(heap, (nd, nxt))
    if b not in dist:
        return None
    path = [b]
    while path[-1] != a:
        path.append(prev[path[-1]])
    path.reverse()
    out, run = [], 0.0
    for i, p in enumerate(path):
        if i:
            run += haversine_m(path[i - 1][0], path[i - 1][1], p[0], p[1])
        out.append((p[0], p[1], run))
    return out


def _point_along(path, fraction):
    """Interpolate a point at `fraction` of the way along a routed path."""
    total = path[-1][2]
    if total <= 0:
        return path[0][0], path[0][1], None
    target = total * fraction
    for i in range(1, len(path)):
        if path[i][2] >= target:
            y1, x1, d1 = path[i - 1]
            y2, x2, d2 = path[i]
            span = d2 - d1
            t = 0.0 if span <= 0 else (target - d1) / span
            lat = y1 + (y2 - y1) * t
            lon = x1 + (x2 - x1) * t
            bearing = math.degrees(math.atan2(
                (x2 - x1) * math.cos(math.radians(lat)), y2 - y1)) % 360
            return lat, lon, bearing
    return path[-1][0], path[-1][1], None


def route_between(a_lat, a_lon, b_lat, b_lon, key=None):
    """Cached routing between two stations, by station-pair key."""
    if key and key in _route_cache:
        return _route_cache[key]
    _, nodes = _graph()
    na = _nearest_node(nodes, a_lat, a_lon)
    nb = _nearest_node(nodes, b_lat, b_lon)
    path = _route(na, nb) if (na and nb) else None
    if key:
        _route_cache[key] = path
    return path


def _pick_time(leg):
    """Best available time for a calling point, and whether it is observed."""
    if not leg:
        return None, None
    for key, basis in (("realtimeActual", "actual"),
                       ("realtimeForecast", "forecast"),
                       ("scheduleAdvertised", "schedule"),
                       ("scheduleInternal", "schedule")):
        ts = _parse_dt(leg.get(key))
        if ts:
            return ts, basis
    return None, None


def _calling_points(service):
    """Flatten a service into ordered points with coordinates and times."""
    coords = {r["code"]: r for r in db.rows(
        "SELECT code, name, lat, lon FROM rail_stations")}
    points = []
    for loc in service.get("locations", []):
        codes = (loc.get("location") or {}).get("shortCodes") or []
        code = codes[0] if codes else None
        station = coords.get(code)
        temporal = loc.get("temporalData") or {}
        arr_ts, arr_basis = _pick_time(temporal.get("arrival"))
        dep_ts, dep_basis = _pick_time(temporal.get("departure"))
        if arr_ts is None and dep_ts is None:
            continue
        points.append({
            "code": code,
            "name": (loc.get("location") or {}).get("description"),
            "lat": station["lat"] if station else None,
            "lon": station["lon"] if station else None,
            "arr": arr_ts or dep_ts,
            "dep": dep_ts or arr_ts,
            "basis": dep_basis or arr_basis,
            "cancelled": bool((temporal.get("departure") or {}).get("isCancelled")
                              or (temporal.get("arrival") or {}).get("isCancelled")),
        })
    return points


def position_from_service(service, now=None):
    """Estimate where one train is. Returns a dict, or None if not placeable."""
    now = now or int(time.time())
    points = _calling_points(service)
    if len(points) < 2:
        return None

    meta = service.get("scheduleMetadata") or {}
    operator = (meta.get("operator") or {})

    # Which leg are we in?
    at = leg = None
    for i, p in enumerate(points):
        if p["arr"] <= now <= p["dep"] and p["lat"] is not None:
            at = p
            break
        if i + 1 < len(points) and p["dep"] <= now <= points[i + 1]["arr"]:
            leg = (p, points[i + 1])
            break

    base = {
        "uid": meta.get("uniqueIdentity"),
        "headcode": meta.get("trainReportingIdentity"),
        "operator": operator.get("name"),
        "origin": (points[0] or {}).get("name"),
        "destination": (points[-1] or {}).get("name"),
    }

    if at is not None:
        return dict(base, lat=at["lat"], lon=at["lon"], bearing=None,
                    from_code=at["code"], to_code=at["code"],
                    from_name=at["name"], to_name=at["name"],
                    progress=0.0, state="at_station", basis=at["basis"],
                    snapped_m=None, leg_start_ts=at["arr"], leg_end_ts=at["dep"])

    if leg is None:
        return None
    a, b = leg
    if a["lat"] is None or b["lat"] is None:
        return None                      # a calling point we have no coordinates for
    span = max(1, b["arr"] - a["dep"])
    frac = min(1.0, max(0.0, (now - a["dep"]) / span))

    # Follow the railway between the two calling points where we can route it.
    # A straight line between distant stations can sit kilometres off the
    # actual line, which is exactly where a naive estimate looks worst.
    moved = None
    path = route_between(a["lat"], a["lon"], b["lat"], b["lon"],
                         key=(a["code"], b["code"]))
    if path and len(path) > 1:
        lat, lon, bearing = _point_along(path, frac)
        moved = 0.0                       # already on the track by construction
    else:
        lat = a["lat"] + (b["lat"] - a["lat"]) * frac
        lon = a["lon"] + (b["lon"] - a["lon"]) * frac
        bearing = math.degrees(math.atan2(
            (b["lon"] - a["lon"]) * math.cos(math.radians(lat)),
            b["lat"] - a["lat"])) % 360
        # No route: fall back to nudging the straight-line point onto nearby
        # track, but only if something is close enough to be plausible.
        snapped_lat, snapped_lon, dist = _snap(lat, lon)
        if dist is not None and dist <= config.TRACK_SNAP_MAX_M:
            lat, lon, moved = snapped_lat, snapped_lon, dist

    return dict(base, lat=lat, lon=lon, bearing=bearing,
                from_code=a["code"], to_code=b["code"],
                from_name=a["name"], to_name=b["name"],
                progress=frac, state="between", basis=a["basis"],
                snapped_m=moved, leg_start_ts=a["dep"], leg_end_ts=b["arr"])


def fetch_service(unique_identity, token):
    """One service's full calling pattern. The namespace prefix is implied."""
    ident = unique_identity.split(":", 1)[-1] if unique_identity.startswith("gb-nr:") \
        else unique_identity
    path = "/gb-nr/service?" + urllib.parse.urlencode({"uniqueIdentity": ident})
    payload, headers = _request(path, token)
    return payload.get("service"), headers


def position_trains():
    """Estimate positions for services currently near Bristol."""
    if not config.TRAIN_POSITIONS:
        return {"ok": False, "error": "disabled"}
    try:
        token = access_token()
    except RailUnavailable as exc:
        return {"ok": False, "error": str(exc)}

    refresh_track()
    now = int(time.time())

    # Services seen on a recent board, most imminent first.
    # Nearest to now first: a service due at a Bristol station in the next few
    # minutes is close by, whereas one due in an hour is still far away and
    # less interesting on this map.
    candidates = db.rows(
        "SELECT DISTINCT uid, ABS(scheduled_ts - ?) AS closeness FROM rail_services "
        "WHERE seen_ts > ? AND is_cancelled = 0 AND scheduled_ts BETWEEN ? AND ? "
        "ORDER BY closeness LIMIT ?",
        (now, now - 1800, now - 3600, now + 3600, config.TRAIN_MAX_SERVICES))
    if not candidates:
        return {"ok": True, "positioned": 0, "candidates": 0}

    rows, placed, budget_stop = [], 0, False
    for c in candidates:
        try:
            service, headers = fetch_service(c["uid"], token)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                budget_stop = True
                break
            continue
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            continue

        # Stop before exhausting the hourly allowance; boards matter more.
        left = headers.get("X-RateLimit-Remaining-Hour")
        if left is not None and int(left) < config.RAIL_RESERVE:
            budget_stop = True
            break

        if not service:
            continue
        pos = position_from_service(service, now)
        if not pos:
            continue
        delay = None
        d = db.one("SELECT delay_min FROM rail_services WHERE uid=? "
                   "ORDER BY seen_ts DESC LIMIT 1", (c["uid"],))
        if d:
            delay = d["delay_min"]
        rows.append((pos["uid"] or c["uid"], pos["headcode"], pos["operator"],
                     pos["origin"], pos["destination"], pos["lat"], pos["lon"],
                     pos["bearing"], pos["from_code"], pos["to_code"],
                     pos["from_name"], pos["to_name"], pos["progress"], delay,
                     pos["state"], pos["basis"], pos["snapped_m"],
                     pos["leg_start_ts"], pos["leg_end_ts"], now))
        placed += 1

    conn = db.connect()
    conn.execute("DELETE FROM train_positions WHERE computed_ts < ?", (now - 1800,))
    if rows:
        conn.executemany(
            "INSERT INTO train_positions(uid, headcode, operator, origin, destination, "
            "lat, lon, bearing, from_code, to_code, from_name, to_name, progress, "
            "delay_min, state, basis, snapped_m, leg_start_ts, leg_end_ts, computed_ts) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(uid) DO UPDATE SET "
            " lat=excluded.lat, lon=excluded.lon, bearing=excluded.bearing,"
            " from_code=excluded.from_code, to_code=excluded.to_code,"
            " from_name=excluded.from_name, to_name=excluded.to_name,"
            " progress=excluded.progress, delay_min=excluded.delay_min,"
            " state=excluded.state, basis=excluded.basis, snapped_m=excluded.snapped_m,"
            " leg_start_ts=excluded.leg_start_ts, leg_end_ts=excluded.leg_end_ts,"
            " computed_ts=excluded.computed_ts", rows)
    conn.commit()
    print(f"[rail] positioned {placed} trains of {len(candidates)} candidates"
          + (" (rate budget reached)" if budget_stop else ""), flush=True)
    return {"ok": True, "positioned": placed, "candidates": len(candidates)}
