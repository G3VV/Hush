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
import re
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


# --- SIRI-VM: route numbers and journey names ---------------------------------
#
# The open GTFS-RT feed identifies a route only by an internal id. SIRI-VM,
# which needs a (free) API key, carries PublishedLineName -- the number on the
# front of the bus -- plus origin and destination. Vehicle references match
# between the two feeds, so this simply enriches what is already tracked.

def _siri_text(block, tag):
    m = re.search(r"<%s>(.*?)</%s>" % (tag, tag), block, re.S)
    return m.group(1).strip() if m else None


def poll_siri(conn=None):
    """Fast path: Bristol-only positions from SIRI-VM, when a key is present.

    The national GTFS-RT file is ~2 MB and covers the whole country; SIRI-VM
    with a bounding box is a fifth of that and only Bristol, so it can be
    polled far more often. It also carries the route number and bearing, so it
    supersedes the GTFS-RT path entirely when a key is configured.
    """
    if not config.BODS_API_KEY:
        return None
    started = time.time()
    now = int(started)
    lo_la, lo_lo, hi_la, hi_lo = config.BBOX
    url = (config.BODS_SIRI_URL + "?" + urllib.parse.urlencode({
        "api_key": config.BODS_API_KEY,
        "boundingBox": f"{lo_lo},{lo_la},{hi_lo},{hi_la}",
    }))
    try:
        raw = _fetch(url, timeout=60).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[transit] SIRI poll failed: {exc}", flush=True)
        return None

    records = []
    for block in re.findall(r"<VehicleActivity>(.*?)</VehicleActivity>", raw, re.S):
        vid = _siri_text(block, "VehicleRef")
        la, lo = _siri_text(block, "Latitude"), _siri_text(block, "Longitude")
        if not vid or la is None or lo is None:
            continue
        try:
            lat, lon = float(la), float(lo)
        except ValueError:
            continue
        brg = _siri_text(block, "Bearing")
        try:
            brg = float(brg) if brg is not None else None
        except ValueError:
            brg = None
        records.append({
            "vehicle_id": vid, "lat": lat, "lon": lon,
            "bearing": brg if (brg is None or brg >= 0) else None,
            "timestamp": _parse_siri_time(_siri_text(block, "RecordedAtTime")),
            "line": _siri_text(block, "PublishedLineName") or _siri_text(block, "LineRef"),
            "direction": _siri_text(block, "DirectionRef"),
            "origin": (_siri_text(block, "OriginName") or "").replace("_", " ") or None,
            "destination": (_siri_text(block, "DestinationName") or "").replace("_", " ") or None,
            "journey": _siri_text(block, "DatedVehicleJourneyRef"),
            "route_id": _siri_text(block, "LineRef"),
        })
    return _store(records, now, conn, source="siri",
                  took_ms=int((time.time() - started) * 1000))


def _parse_siri_time(value):
    if not value:
        return None
    try:
        return int(time.mktime(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return None


def _store(records, now, conn=None, source="siri", took_ms=0):
    """Shared upsert for both feeds: positions, tracks and the sample row."""
    conn = conn or db.connect()
    prev = {r["vehicle_id"]: r for r in db.rows(
        "SELECT vehicle_id, lat, lon, last_seen FROM transit_vehicles")}

    rows, positions, speeds = [], [], []
    routes, operators = set(), set()
    fresh = 0

    for r in records:
        reported = r.get("timestamp")
        if reported and now - reported > config.TRANSIT_MAX_AGE_S:
            continue
        fresh += 1
        vid = r["vehicle_id"]
        operator = _operator_of(vid)
        if operator:
            operators.add(operator)
        if r.get("line"):
            routes.add(r["line"])

        speed, dist = None, 0.0
        p = prev.get(vid)
        if p and p["lat"] is not None:
            dist = haversine_m(p["lat"], p["lon"], r["lat"], r["lon"])
            gap = now - (p["last_seen"] or now)
            if gap > 0 and dist > 5:
                speed = (dist / 1000.0) / (gap / 3600.0)
                if speed > 120:
                    speed, dist = None, 0.0
                else:
                    speeds.append(speed)

        rows.append((vid, operator, "bus", r.get("route_id"), r.get("journey"),
                     None, r["lat"], r["lon"], r.get("bearing"), speed, reported,
                     now, now, dist, r.get("line"), r.get("direction"),
                     r.get("origin"), r.get("destination"), r.get("journey")))
        positions.append((vid, now, r["lat"], r["lon"], r.get("bearing"), speed))

    if not rows:
        return {"ok": True, "live": 0}

    conn.execute("BEGIN")
    try:
        conn.executemany(
            "INSERT INTO transit_vehicles(vehicle_id, operator, mode, route_id, "
            "trip_id, start_time, lat, lon, bearing, speed_kmh, reported_ts, "
            "first_seen, last_seen, distance_m, line_name, direction, origin_name, "
            "destination_name, journey_ref, fixes) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1) "
            "ON CONFLICT(vehicle_id) DO UPDATE SET "
            " operator=excluded.operator, route_id=excluded.route_id,"
            " lat=excluded.lat, lon=excluded.lon, bearing=excluded.bearing,"
            " speed_kmh=excluded.speed_kmh, reported_ts=excluded.reported_ts,"
            " last_seen=excluded.last_seen,"
            " distance_m=transit_vehicles.distance_m+excluded.distance_m,"
            " line_name=COALESCE(excluded.line_name, transit_vehicles.line_name),"
            " direction=COALESCE(excluded.direction, transit_vehicles.direction),"
            " origin_name=COALESCE(excluded.origin_name, transit_vehicles.origin_name),"
            " destination_name=COALESCE(excluded.destination_name, transit_vehicles.destination_name),"
            " journey_ref=COALESCE(excluded.journey_ref, transit_vehicles.journey_ref),"
            " fixes=transit_vehicles.fixes+1", rows)
        conn.executemany(
            "INSERT INTO transit_positions(vehicle_id, ts, lat, lon, bearing, speed_kmh) "
            "VALUES(?,?,?,?,?,?)", positions)
        conn.execute(
            "INSERT OR REPLACE INTO transit_samples(ts, active, moving, operators, "
            "routes, mean_speed_kmh, feed_age_s) VALUES(?,?,?,?,?,?,?)",
            (now, fresh, sum(1 for s in speeds if s > 3), len(operators), len(routes),
             (sum(speeds) / len(speeds)) if speeds else None, None))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    print(f"[transit] {time.strftime('%H:%M:%S')} {fresh} live via {source} "
          f"({len(routes)} routes) {took_ms}ms", flush=True)
    return {"ok": True, "live": fresh, "routes": len(routes)}


def refresh_line_names(conn=None):
    if not config.BODS_API_KEY:
        return 0
    lo_la, lo_lo, hi_la, hi_lo = config.BBOX
    url = (config.BODS_SIRI_URL + "?" + urllib.parse.urlencode({
        "api_key": config.BODS_API_KEY,
        "boundingBox": f"{lo_lo},{lo_la},{hi_lo},{hi_la}",
    }))
    try:
        raw = _fetch(url, timeout=90).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[transit] SIRI feed failed: {exc}", flush=True)
        return 0

    rows = []
    for block in re.findall(r"<VehicleActivity>(.*?)</VehicleActivity>", raw, re.S):
        vid = _siri_text(block, "VehicleRef")
        if not vid:
            continue
        rows.append((
            _siri_text(block, "PublishedLineName") or _siri_text(block, "LineRef"),
            _siri_text(block, "DirectionRef"),
            (_siri_text(block, "OriginName") or "").replace("_", " ") or None,
            (_siri_text(block, "DestinationName") or "").replace("_", " ") or None,
            _siri_text(block, "DatedVehicleJourneyRef"),
            vid,
        ))
    if not rows:
        return 0
    conn = conn or db.connect()
    conn.executemany(
        "UPDATE transit_vehicles SET line_name=?, direction=?, origin_name=?, "
        "destination_name=?, journey_ref=? WHERE vehicle_id=?", rows)
    conn.commit()
    named = conn.execute(
        "SELECT COUNT(*) FROM transit_vehicles WHERE line_name IS NOT NULL").fetchone()[0]
    print(f"[transit] SIRI: {len(rows)} journeys, {named} vehicles named", flush=True)
    return len(rows)


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


# --- bus route geometry --------------------------------------------------------
#
# TransXChange timetables carry the actual road alignment: RouteSection holds
# RouteLinks, each with a Track of coordinates. Chaining
# VehicleJourney -> JourneyPattern -> Route -> RouteSection gives a polyline per
# line and direction, which is what lets a bus's remaining path be drawn rather
# than guessed. Needs a (free) BODS API key.

TXC_NS = "{http://www.transxchange.org.uk/}"


def _parse_transxchange(fh):
    """Extract {(line_name, direction): [(lat, lon), ...]} from one TXC file."""
    import xml.etree.ElementTree as ET
    sections, routes, lines, jps, journeys = {}, {}, {}, {}, []
    try:
        for _, el in ET.iterparse(fh, events=("end",)):
            tag = el.tag.replace(TXC_NS, "")
            if tag == "RouteSection":
                pts = []
                for loc in el.iter(TXC_NS + "Location"):
                    la, lo = loc.find(TXC_NS + "Latitude"), loc.find(TXC_NS + "Longitude")
                    if la is not None and lo is not None:
                        try:
                            pts.append((float(la.text), float(lo.text)))
                        except (TypeError, ValueError):
                            pass
                sections[el.get("id")] = pts
                el.clear()
            elif tag == "Route":
                routes[el.get("id")] = [r.text for r in el.iter(TXC_NS + "RouteSectionRef")]
                el.clear()
            elif tag == "Line":
                nm = el.find(TXC_NS + "LineName")
                if nm is not None:
                    lines[el.get("id")] = (nm.text or "").strip()
                el.clear()
            elif tag == "JourneyPattern":
                rr = el.find(TXC_NS + "RouteRef")
                di = el.find(TXC_NS + "Direction")
                if rr is not None:
                    jps[el.get("id")] = (rr.text, (di.text or "").strip() if di is not None else None)
                el.clear()
            elif tag == "VehicleJourney":
                lr = el.find(TXC_NS + "LineRef")
                jr = el.find(TXC_NS + "JourneyPatternRef")
                if lr is not None and jr is not None:
                    journeys.append((lr.text, jr.text))
                el.clear()
    except ET.ParseError:
        return {}

    out = {}
    for line_ref, jp_ref in journeys:
        jp = jps.get(jp_ref)
        if not jp:
            continue
        route_ref, direction = jp
        name = lines.get(line_ref) or line_ref
        coords = []
        for sec in routes.get(route_ref, []):
            coords.extend(sections.get(sec, []))
        if len(coords) < 10:
            continue
        key = (name, (direction or "outbound").lower())
        # Several journeys share a route; keep the most complete alignment.
        if len(coords) > len(out.get(key, [])):
            out[key] = coords
    return out


def refresh_route_shapes(force=False, max_datasets=6):
    """Download Bristol-area timetables and store one polyline per line."""
    if not config.BODS_API_KEY:
        return 0
    now = int(time.time())
    newest = db.scalar("SELECT MAX(fetched_at) FROM bus_routes", default=0) or 0
    if not force and now - newest < config.OSM_REFRESH_S:
        return 0

    url = ("https://data.bus-data.dft.gov.uk/api/v1/dataset/?" +
           urllib.parse.urlencode({"api_key": config.BODS_API_KEY,
                                   "limit": max_datasets, "search": "Bristol"}))
    try:
        catalogue = json.loads(_fetch(url, timeout=90).decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"[transit] timetable catalogue failed: {exc}", flush=True)
        return 0

    lo_la, lo_lo, hi_la, hi_lo = config.BBOX
    pad = 0.35          # keep routes that leave the city but serve it

    def near_bristol(pts):
        """Line numbers repeat across the country, so anchor to our area."""
        inside = sum(1 for la, lo in pts
                     if lo_la - pad <= la <= hi_la + pad
                     and lo_lo - pad <= lo <= hi_lo + pad)
        return inside >= max(5, len(pts) * 0.15)

    shapes = {}
    for ds in (catalogue.get("results") or [])[:max_datasets]:
        link = ds.get("url")
        if not link:
            continue
        sep = "&" if "?" in link else "?"
        try:
            blob = _fetch(link + sep + "api_key=" + config.BODS_API_KEY, timeout=240)
            with zipfile.ZipFile(io.BytesIO(blob)) as z:
                for name in z.namelist():
                    if not name.endswith(".xml"):
                        continue
                    with z.open(name) as fh:
                        for key, pts in _parse_transxchange(fh).items():
                            if not near_bristol(pts):
                                continue
                            if len(pts) > len(shapes.get(key, [])):
                                shapes[key] = pts
        except (urllib.error.URLError, TimeoutError, OSError, ValueError,
                zipfile.BadZipFile) as exc:
            print(f"[transit] timetable {ds.get('operatorName')} failed: {exc}", flush=True)
            continue
        print(f"[transit] parsed {ds.get('operatorName')}: {len(shapes)} shapes so far",
              flush=True)

    if not shapes:
        return 0
    conn = db.connect()
    conn.executemany(
        "INSERT INTO bus_routes(line_name, direction, points, n_points, fetched_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(line_name, direction) DO UPDATE SET "
        "points=excluded.points, n_points=excluded.n_points, "
        "fetched_at=excluded.fetched_at",
        [(k[0], k[1], json.dumps([[round(a, 5), round(b, 5)] for a, b in v]),
          len(v), now) for k, v in shapes.items()])
    conn.commit()
    print(f"[transit] route shapes: {len(shapes)} line/direction pairs", flush=True)
    return len(shapes)
