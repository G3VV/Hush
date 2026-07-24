"""Public transport: live buses and coaches, plus static infrastructure.

Two sources, both open and neither needing an API key:

  * DfT Bus Open Data Service (BODS) publishes every operator's real-time
    vehicle positions nationwide as a single GTFS-Realtime file. We fetch it,
    clip to the Bristol bounding box and keep what is fresh.
  * OpenStreetMap, via Overpass, for rail stations, bus stops, coach stations
    and ferry piers. Static, so it is cached for a week.

Unlike the Dott scooters, bus vehicle ids are stable fleet identifiers, so
these vehicles genuinely can be followed from poll to poll: their tracks are
recorded and drawn.

What is *not* here: live train positions. Every UK source for those (Network
Rail's feeds, National Rail Darwin, Realtime Trains) requires a registered
account, so rail appears as stations rather than moving vehicles. See the
README.
"""

import io
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

from . import config, db, gtfsrt
from .collector import haversine_m

# Overpass through a proxy is fussy: it needs an explicit JSON Accept header,
# rejects union syntax over GET, and rate-limits hard. One simple query each.
OSM_QUERIES = {
    "rail_station":   "node[railway=station]",
    "rail_halt":      "node[railway=halt]",
    "bus_station":    "node[amenity=bus_station]",
    "ferry_terminal": "node[amenity=ferry_terminal]",
    "bus_stop":       "node[highway=bus_stop]",
}

# Vehicle id prefixes seen in the BODS feed map to operator names.
OPERATORS = {
    "FBRI": "First West of England",
    "SSWL": "Stagecoach West",
    "EUTX": "Eurotaxis",
    "ABUS": "Abus",
    "BALS": "Bakers Dolphin",
    "CTPU": "CT Plus",
    "WBTR": "Westbus",
    "TFBS": "Transport for Bristol",
}


def _fetch(url, timeout=None, headers=None):
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": config.USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout or config.HTTP_TIMEOUT_S) as resp:
        return resp.read()


def fetch_vehicles():
    """Download the national GTFS-RT feed and return Bristol-area vehicles."""
    raw = _fetch(config.BODS_GTFSRT_URL, timeout=120)
    # BODS serves the protobuf inside a zip.
    if raw[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            name = next((n for n in z.namelist() if n.endswith(".bin")), z.namelist()[0])
            raw = z.read(name)
    return gtfsrt.vehicle_positions(raw, config.BBOX)


def _operator_of(vehicle_id):
    if not vehicle_id or "-" not in vehicle_id:
        return None
    prefix = vehicle_id.split("-", 1)[0]
    # Only treat it as an operator code if it looks like one: a registration
    # plate split on a hyphen would give nonsense.
    if 3 <= len(prefix) <= 5 and prefix.isalpha() and prefix.isupper():
        return OPERATORS.get(prefix, prefix)
    return None


def poll(conn=None):
    """One transit poll: fetch, store positions, update tracks."""
    started = time.time()
    now = int(started)
    try:
        vehicles = fetch_vehicles()
    except (urllib.error.URLError, TimeoutError, ValueError, OSError,
            zipfile.BadZipFile) as exc:
        print(f"[transit] fetch failed: {exc}", flush=True)
        return {"ok": False, "error": str(exc)}

    conn = conn or db.connect()
    prev = {r["vehicle_id"]: r for r in db.rows(
        "SELECT vehicle_id, lat, lon, last_seen, distance_m, fixes FROM transit_vehicles")}

    fresh, rows, positions = 0, [], []
    speeds, routes, operators = [], set(), set()
    ages = []

    for v in vehicles:
        vid = v.get("vehicle_id")
        if not vid:
            continue
        reported = v.get("timestamp")
        if reported:
            age = now - reported
            ages.append(age)
            # Operators leave dead vehicles in the feed for hours; drawing them
            # would be inventing buses that are not there.
            if age > config.TRANSIT_MAX_AGE_S:
                continue
        fresh += 1

        bearing = v.get("bearing")
        if bearing is not None and bearing < 0:
            bearing = None            # -1 is the feed's "unknown" sentinel

        lat, lon = v["lat"], v["lon"]
        route = (v.get("route_id") or "").strip() or None
        operator = _operator_of(vid)
        if route:
            routes.add(route)
        if operator:
            operators.add(operator)

        p = prev.get(vid)
        speed = None
        dist = 0.0
        if p and p["lat"] is not None:
            dist = haversine_m(p["lat"], p["lon"], lat, lon)
            gap = now - (p["last_seen"] or now)
            if gap > 0 and dist > 5:
                speed = (dist / 1000.0) / (gap / 3600.0)
                # Anything above this is a GPS jump, not a bus.
                if speed > 120:
                    speed, dist = None, 0.0
                else:
                    speeds.append(speed)

        rows.append((vid, operator, "bus", route, v.get("trip_id"),
                     v.get("start_time"), lat, lon, bearing, speed, reported,
                     now, now, dist))
        positions.append((vid, now, lat, lon, bearing, speed))

    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT INTO transit_vehicles(vehicle_id, operator, mode, route_id, "
            "trip_id, start_time, lat, lon, bearing, speed_kmh, reported_ts, "
            "first_seen, last_seen, distance_m, fixes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(vehicle_id) DO UPDATE SET "
            " operator=excluded.operator, route_id=excluded.route_id,"
            " trip_id=excluded.trip_id, start_time=excluded.start_time,"
            " lat=excluded.lat, lon=excluded.lon, bearing=excluded.bearing,"
            " speed_kmh=excluded.speed_kmh, reported_ts=excluded.reported_ts,"
            " last_seen=excluded.last_seen,"
            " distance_m=transit_vehicles.distance_m+excluded.distance_m,"
            " fixes=transit_vehicles.fixes+1",
            rows)
        conn.executemany(
            "INSERT INTO transit_positions(vehicle_id, ts, lat, lon, bearing, speed_kmh) "
            "VALUES(?,?,?,?,?,?)", positions)
        moving = sum(1 for s in speeds if s > 3)
        conn.execute(
            "INSERT OR REPLACE INTO transit_samples(ts, active, moving, operators, "
            "routes, mean_speed_kmh, feed_age_s) VALUES(?,?,?,?,?,?,?)",
            (now, fresh, moving, len(operators), len(routes),
             (sum(speeds) / len(speeds)) if speeds else None,
             int(sorted(ages)[len(ages) // 2]) if ages else None))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    print(f"[transit] {time.strftime('%H:%M:%S')} {fresh} live "
          f"({len(vehicles)} in bbox) {len(operators)} operators "
          f"{int((time.time()-started)*1000)}ms", flush=True)
    return {"ok": True, "live": fresh, "in_bbox": len(vehicles),
            "operators": len(operators), "routes": len(routes)}


def prune(conn=None):
    conn = conn or db.connect()
    cutoff = int(time.time()) - config.TRANSIT_TRAIL_S
    conn.execute("DELETE FROM transit_positions WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM transit_vehicles WHERE last_seen < ?",
                 (int(time.time()) - 86400,))
    conn.commit()


# --- static infrastructure -----------------------------------------------------

def refresh_osm(force=False):
    """Fetch stations, stops and piers from Overpass. Cached for a week."""
    now = int(time.time())
    lo_la, lo_lo, hi_la, hi_lo = config.BBOX
    bbox = f"({lo_la},{lo_lo},{hi_la},{hi_lo})"
    conn = db.connect()
    total = 0

    # Freshness is tracked per kind. Overpass rate-limits and times out often
    # enough that a global check would let one failed query block that kind for
    # a whole refresh period while the others sat fresh.
    fresh = {r["kind"]: r["at"] for r in db.rows(
        "SELECT kind, MAX(fetched_at) AS at FROM osm_features GROUP BY kind")}

    for kind, selector in OSM_QUERIES.items():
        if not force and now - fresh.get(kind, 0) < config.OSM_REFRESH_S:
            continue
        query = f"[out:json][timeout:60];{selector}{bbox};out body;"
        url = config.OVERPASS_URL + "?data=" + urllib.parse.quote(query, safe="")
        try:
            raw = _fetch(url, timeout=120, headers={
                "Accept": "application/json", "User-Agent": config.USER_AGENT})
            payload = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            print(f"[transit] overpass {kind} failed: {exc}", flush=True)
            continue

        rows = []
        for el in payload.get("elements", []):
            if el.get("lat") is None:
                continue
            rows.append((el["id"], kind, (el.get("tags") or {}).get("name"),
                         el["lat"], el["lon"], now))
        if rows:
            conn.executemany(
                "INSERT INTO osm_features(osm_id, kind, name, lat, lon, fetched_at) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(osm_id) DO UPDATE SET "
                "kind=excluded.kind, name=excluded.name, lat=excluded.lat, "
                "lon=excluded.lon, fetched_at=excluded.fetched_at", rows)
            conn.commit()
            total += len(rows)
        print(f"[transit] osm {kind}: {len(rows)}", flush=True)
        time.sleep(12)     # Overpass returns 429 if queries come back to back

    return total
