"""Aggregations over the collected fleet history.

A note on what is knowable. Dott rotates `bike_id` after every rental (the GBFS
spec asks operators to, so that riders cannot be followed between trips). So a
vehicle that leaves the feed never returns under the same id. We can observe
that a rental STARTED at one place and that another ENDED somewhere else, but
never which start belongs to which end. Nothing here pretends otherwise:
origin-destination pairs are not reconstructed, and ride duration is derived
statistically rather than measured per vehicle.
"""

import json
import time

from . import db
from .collector import haversine_m

# Bristol pricing at time of writing: GBP 1 unlock + GBP 0.25/min, both modes.
UNLOCK_GBP = 1.00
PER_MIN_GBP = 0.25

# Counting how many vehicles are out being ridden is not as simple as counting
# the ones missing from the feed: because ids retire on rental, missing ids pile
# up forever. Instead we take the largest available-count ever seen as the size
# of the deployed fleet (at the quietest hour nearly everything is parked), and
# read concurrent rides as the shortfall against it. That needs to have seen at
# least one overnight lull to be meaningful.
MIN_SPAN_FOR_RIDE_EST_S = 12 * 3600
MIN_PICKUPS_FOR_RIDE_EST = 100


def _hist(values, edges):
    """Bucket values into [edges[i], edges[i+1]) plus a final overflow bucket."""
    out = [0] * len(edges)
    for v in values:
        if v is None:
            continue
        for i in range(len(edges) - 1, -1, -1):
            if v >= edges[i]:
                out[i] += 1
                break
    return out


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def live_vehicles(vtype=None, min_fuel=None, max_fuel=None, status=None, limit=6000):
    sql = ("SELECT bike_id, vehicle_type_id, lat, lon, fuel, range_m, is_disabled, "
           "is_reserved, parked_since, last_seen, last_reported, trip_count, "
           "trip_distance_m FROM vehicles WHERE present=1")
    args = []
    if vtype in ("dott_scooter", "dott_bicycle"):
        sql += " AND vehicle_type_id=?"
        args.append(vtype)
    if min_fuel is not None:
        sql += " AND fuel >= ?"
        args.append(min_fuel / 100.0)
    if max_fuel is not None:
        sql += " AND fuel <= ?"
        args.append(max_fuel / 100.0)
    if status == "disabled":
        sql += " AND is_disabled=1"
    elif status == "reserved":
        sql += " AND is_reserved=1"
    sql += " LIMIT ?"
    args.append(limit)

    now = int(time.time())
    out = []
    for r in db.rows(sql, tuple(args)):
        out.append({
            "id": r["bike_id"],
            "t": 1 if r["vehicle_type_id"] == "dott_bicycle" else 0,
            "lat": round(r["lat"], 6),
            "lon": round(r["lon"], 6),
            "f": round((r["fuel"] or 0) * 100),
            "km": round((r["range_m"] or 0) / 1000.0, 1),
            "d": r["is_disabled"] or 0,
            "r": r["is_reserved"] or 0,
            "idle": now - (r["parked_since"] or now),
            "trips": r["trip_count"] or 0,
        })
    return out


def vehicle_detail(bike_id, history_limit=60):
    """Everything observable about one vehicle *under its current id*.

    The id is retired the moment someone rents it, so this history covers the
    current parked spell: how long it has sat, how the battery has drained, and
    any operational moves (which keep the id, since no rental took place).
    """
    v = db.one("SELECT * FROM vehicles WHERE bike_id=?", (bike_id,))
    if not v:
        return None
    now = int(time.time())
    moves = db.rows(
        "SELECT id, start_lat, start_lon, end_lat, end_lon, start_ts, end_ts, "
        "duration_s, distance_m, fuel_start, fuel_end, fuel_delta, kind, confidence "
        "FROM trips WHERE bike_id=? ORDER BY end_ts DESC LIMIT ?",
        (bike_id, history_limit),
    )
    stays = db.rows(
        "SELECT id, lat, lon, start_ts, end_ts, start_fuel, end_fuel, is_open "
        "FROM parkings WHERE bike_id=? ORDER BY start_ts DESC LIMIT ?",
        (bike_id, history_limit),
    )
    total_m = sum(m["distance_m"] for m in moves)
    first = v["first_seen"] or now
    drain = None
    open_stay = next((s for s in stays if s["is_open"]), None)
    if open_stay and open_stay["start_fuel"] is not None and v["fuel"] is not None:
        hours = max(1.0, (now - open_stay["start_ts"]) / 3600.0)
        drain = round((open_stay["start_fuel"] - v["fuel"]) * 100 / hours, 2)

    return {
        "id": v["bike_id"],
        "type": v["vehicle_type_id"],
        "present": bool(v["present"]),
        "lat": v["lat"], "lon": v["lon"],
        "fuel": v["fuel"], "range_m": v["range_m"],
        "is_disabled": bool(v["is_disabled"]),
        "is_reserved": bool(v["is_reserved"]),
        "first_seen": first, "last_seen": v["last_seen"],
        "last_reported": v["last_reported"],
        "parked_since": v["parked_since"],
        "idle_s": now - (v["parked_since"] or now) if v["present"] else None,
        "tracked_s": now - first,
        "gone_since": v["last_seen"] if not v["present"] else None,
        "battery_drain_pct_per_h": drain,
        "move_count": len(moves),
        "total_distance_m": total_m,
        "rental_uri": f"https://go.ridedott.com/vehicles/{v['bike_id']}",
        "moves": moves,
        "stays": stays,
    }


def overview(hours=24):
    now = int(time.time())
    since = now - hours * 3600

    present = db.scalar("SELECT COUNT(*) FROM vehicles WHERE present=1", default=0)
    # Deployed fleet size, taken as the high-water mark of availability.
    peak_available = db.scalar("SELECT MAX(available) FROM fleet_samples", default=present) or present
    riding = max(0, peak_available - present)
    scooters = db.scalar(
        "SELECT COUNT(*) FROM vehicles WHERE present=1 AND vehicle_type_id='dott_scooter'",
        default=0)
    bikes = db.scalar(
        "SELECT COUNT(*) FROM vehicles WHERE present=1 AND vehicle_type_id='dott_bicycle'",
        default=0)
    disabled = db.scalar(
        "SELECT COUNT(*) FROM vehicles WHERE present=1 AND is_disabled=1", default=0)
    avg_fuel = db.scalar("SELECT AVG(fuel) FROM vehicles WHERE present=1", default=0) or 0
    low = db.scalar("SELECT COUNT(*) FROM vehicles WHERE present=1 AND fuel < 0.2", default=0)

    fuels = [r["fuel"] * 100 for r in db.rows(
        "SELECT fuel FROM vehicles WHERE present=1 AND fuel IS NOT NULL")]
    battery_hist = _hist(fuels, [0, 10, 20, 30, 40, 50, 60, 70, 80, 90])

    idles = [(now - r["parked_since"]) / 3600.0 for r in db.rows(
        "SELECT parked_since FROM vehicles WHERE present=1 AND parked_since IS NOT NULL")]
    idle_hist = _hist(idles, [0, 1, 3, 6, 12, 24, 48, 72])

    # --- rental events ---------------------------------------------------
    events = db.rows(
        "SELECT ts, kind, dwell_s, vehicle_type_id FROM events WHERE ts >= ?", (since,))
    pickups = [e for e in events if e["kind"] == "pickup"]
    dropoffs = [e for e in events if e["kind"] == "dropoff"]

    first_poll = db.scalar("SELECT MIN(ts) FROM polls WHERE ok=1")
    span = max(1, now - max(since, first_poll or since))

    by_hour = {}
    for e in events:
        h = (e["ts"] // 3600) * 3600
        slot = by_hour.setdefault(h, {"ts": h, "pickups": 0, "dropoffs": 0})
        slot["pickups" if e["kind"] == "pickup" else "dropoffs"] += 1
    hourly = [by_hour[k] for k in sorted(by_hour)]

    samples = db.rows(
        # in_use is still recorded as a raw count but is not exposed: it counts
        # retired ids as well as riding ones, so `riding` below supersedes it.
        "SELECT ts, available, scooters, bicycles, avg_fuel, low_battery "
        "FROM fleet_samples WHERE ts >= ? ORDER BY ts", (since,))

    # Concurrent rides per sample, as the shortfall against the deployed fleet.
    for s in samples:
        s["riding"] = max(0, peak_available - (s["available"] or peak_available))

    # Mean ride duration by Little's Law: with L vehicles out at any moment and
    # rentals starting at rate lambda, the average ride lasts L / lambda. This
    # sidesteps the id rotation entirely -- it needs only counts, never a link
    # between a particular start and a particular end.
    total_observed = (now - first_poll) if first_poll else 0
    mean_riding = (sum(s["riding"] for s in samples) / len(samples)) if samples else riding
    rate_per_s = len(pickups) / span
    reliable = (total_observed >= MIN_SPAN_FOR_RIDE_EST_S
                and len(pickups) >= MIN_PICKUPS_FOR_RIDE_EST)
    est_ride_s = (mean_riding / rate_per_s) if (rate_per_s > 0 and reliable) else None

    dwells = [p["dwell_s"] / 3600.0 for p in pickups if p["dwell_s"] is not None]
    dwell_hist = _hist(dwells, [0, 0.25, 0.5, 1, 2, 4, 8, 24])
    med_dwell = _median([p["dwell_s"] for p in pickups if p["dwell_s"] is not None])

    est_rev = None
    if est_ride_s is not None:
        est_rev = round(len(pickups) * (UNLOCK_GBP + (est_ride_s / 60.0) * PER_MIN_GBP), 2)

    moves = db.rows("SELECT kind FROM trips WHERE end_ts >= ?", (since,))
    kinds = {}
    for m in moves:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1

    # Demand by hour of day, averaged over however many days we have.
    hod = [0] * 24
    for r in db.rows(
        "SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS h, "
        "COUNT(*) AS n FROM events WHERE kind='pickup' AND ts >= ? GROUP BY h",
        (since,)
    ):
        hod[r["h"]] = r["n"]
    days = max(1.0, span / 86400.0)
    hod_avg = [round(n / days, 1) for n in hod]

    # Which mode is actually being rented, versus what is on the street.
    mode_rentals = {"scooter": 0, "bicycle": 0}
    for p in pickups:
        if p["vehicle_type_id"] == "dott_bicycle":
            mode_rentals["bicycle"] += 1
        else:
            mode_rentals["scooter"] += 1
    share_bike_fleet = round(100.0 * bikes / present, 1) if present else 0
    share_bike_rent = round(
        100.0 * mode_rentals["bicycle"] / len(pickups), 1) if pickups else 0

    # How concentrated is the fleet? Distance from the city centre, and the
    # share of rentals coming from the busiest cells.
    clat, clon = 51.4545, -2.5879
    dists = [haversine_m(clat, clon, r["lat"], r["lon"]) / 1000.0 for r in db.rows(
        "SELECT lat, lon FROM vehicles WHERE present=1 AND lat IS NOT NULL")]
    centre_hist = _hist(dists, [0, 1, 2, 3, 5, 8, 12])
    cells = hotspots(hours, "pickup", limit=100000)
    top10 = sum(c["n"] for c in cells[:10])
    concentration = round(100.0 * top10 / len(pickups), 1) if pickups else 0

    poll_count = db.scalar("SELECT COUNT(*) FROM polls WHERE ok=1", default=0)

    return {
        "generated_at": now,
        "window_hours": hours,
        "coverage": {
            "first_poll": first_poll,
            "polls": poll_count,
            "observing_s": (now - first_poll) if first_poll else 0,
            "window_span_s": span,
            "last_poll": db.one("SELECT * FROM polls ORDER BY ts DESC LIMIT 1"),
        },
        "fleet": {
            "available": present,
            "riding": riding,
            "peak_available": peak_available,
            "scooters": scooters,
            "bicycles": bikes,
            "disabled": disabled,
            "avg_fuel_pct": round(avg_fuel * 100, 1),
            "low_battery": low,
            "utilisation_pct": round(100.0 * riding / peak_available, 1) if peak_available else 0.0,
        },
        "activity": {
            "rentals_started": len(pickups),
            "rentals_ended": len(dropoffs),
            "per_hour": round(len(pickups) / (span / 3600.0), 1) if span else 0,
            "per_vehicle_per_day": round(
                (len(pickups) / (span / 86400.0)) / present, 2) if present and span else 0,
            "est_ride_min": round(est_ride_s / 60.0, 1) if est_ride_s else None,
            "est_ride_reliable": reliable,
            "needs_hours_for_estimate": round(
                max(0, MIN_SPAN_FOR_RIDE_EST_S - total_observed) / 3600.0, 1),
            "mean_concurrent_rides": round(mean_riding, 1),
            "median_dwell_s": med_dwell,
            "est_revenue_gbp": est_rev,
            "ops_moves": kinds,
            "mode_rentals": mode_rentals,
            "bike_share_fleet_pct": share_bike_fleet,
            "bike_share_rentals_pct": share_bike_rent,
            "top10_cell_share_pct": concentration,
        },
        "charts": {
            "battery_hist": battery_hist,
            "idle_hist": idle_hist,
            "dwell_hist": dwell_hist,
            "hourly": hourly,
            "hour_of_day": hod_avg,
            "centre_hist": centre_hist,
            "fleet_samples": samples,
        },
        "transit": transit_overview(hours),
        "rail": rail_overview(hours),
        "bus_insights": bus_insights(hours),
        "rail_insights": rail_insights(hours),
    }


# --- public transport ----------------------------------------------------------

def transit_live(max_age_s=None, operator=None, limit=3000):
    """Buses seen recently enough to still be on the road."""
    from . import config
    max_age_s = max_age_s or config.TRANSIT_MAX_AGE_S
    now = int(time.time())
    sql = ("SELECT vehicle_id, operator, route_id, line_name, destination_name, "
           "lat, lon, bearing, speed_kmh, reported_ts, last_seen, distance_m, fixes "
           "FROM transit_vehicles WHERE last_seen > ?")
    args = [now - max_age_s]
    if operator:
        sql += " AND operator = ?"
        args.append(operator)
    sql += " LIMIT ?"
    args.append(limit)
    out = []
    for r in db.rows(sql, tuple(args)):
        out.append({
            "id": r["vehicle_id"],
            "op": r["operator"],
            "route": r["route_id"],
            "line": r["line_name"],
            "dest": r["destination_name"],
            "lat": round(r["lat"], 6),
            "lon": round(r["lon"], 6),
            "brg": round(r["bearing"]) if r["bearing"] is not None else None,
            "kmh": round(r["speed_kmh"], 1) if r["speed_kmh"] is not None else None,
            "age": now - (r["reported_ts"] or r["last_seen"]),
            "km": round((r["distance_m"] or 0) / 1000.0, 1),
        })
    return out


def transit_vehicle(vehicle_id, points=400):
    v = db.one("SELECT * FROM transit_vehicles WHERE vehicle_id=?", (vehicle_id,))
    if not v:
        return None
    now = int(time.time())
    track = db.rows(
        "SELECT ts, lat, lon, bearing, speed_kmh FROM transit_positions "
        "WHERE vehicle_id=? ORDER BY ts DESC LIMIT ?", (vehicle_id, points))
    track.reverse()
    speeds = [p["speed_kmh"] for p in track if p["speed_kmh"] is not None]
    return {
        "id": v["vehicle_id"],
        "operator": v["operator"],
        "mode": v["mode"],
        "route_id": v["route_id"],
        "line_name": v["line_name"],
        "direction": v["direction"],
        "origin_name": v["origin_name"],
        "destination_name": v["destination_name"],
        "trip_id": v["trip_id"],
        "start_time": v["start_time"],
        "lat": v["lat"], "lon": v["lon"],
        "bearing": v["bearing"],
        "speed_kmh": v["speed_kmh"],
        "reported_ts": v["reported_ts"],
        "age_s": now - (v["reported_ts"] or v["last_seen"]),
        "first_seen": v["first_seen"],
        "last_seen": v["last_seen"],
        "tracked_s": now - (v["first_seen"] or now),
        "distance_m": v["distance_m"],
        "fixes": v["fixes"],
        "avg_speed_kmh": round(sum(speeds) / len(speeds), 1) if speeds else None,
        "max_speed_kmh": round(max(speeds), 1) if speeds else None,
        "track": track,
    }


def bus_path(vehicle_id, max_past=400):
    """A bus's journey: where it has been, and the road still ahead.

    Raw GPS fixes make a poor line. Positions arrive every 30-120 seconds, so
    consecutive fixes are hundreds of metres apart and a straight hop between
    them cuts corners, crosses buildings and zigzags with GPS noise. Where we
    hold the route alignment, each fix is projected onto it and the *route*
    between the first and last projection is returned instead. That follows the
    road exactly and is smooth by construction.

    Without an alignment (no BODS key, or an unmatched line) it falls back to
    the raw fixes, cleaned of noise.
    """
    v = db.one("SELECT vehicle_id, line_name, direction, lat, lon, destination_name "
               "FROM transit_vehicles WHERE vehicle_id=?", (vehicle_id,))
    if not v:
        return None

    fixes = [(r["lat"], r["lon"]) for r in db.rows(
        "SELECT lat, lon FROM transit_positions WHERE vehicle_id=? "
        "ORDER BY ts DESC LIMIT ?", (vehicle_id, max_past))][::-1]

    shape = None
    if v["line_name"]:
        want = (v["direction"] or "").lower()
        rows = db.rows("SELECT direction, points FROM bus_routes WHERE line_name=?",
                       (v["line_name"],))
        chosen = next((r for r in rows if r["direction"] == want), None) or \
            (rows[0] if rows else None)
        if chosen:
            pts = json.loads(chosen["points"] or "[]")
            if len(pts) > 1:
                shape = pts

    past, future, snapped = [], [], False

    if shape:
        def nearest(lat, lon):
            best_i, best_d = None, None
            for i, (la, lo) in enumerate(shape):
                d = (la - lat) ** 2 + (lo - lon) ** 2
                if best_d is None or d < best_d:
                    best_i, best_d = i, d
            return best_i, haversine_m(lat, lon, shape[best_i][0], shape[best_i][1])

        here_i, here_m = nearest(v["lat"], v["lon"]) if v["lat"] is not None else (None, None)
        if here_i is not None and here_m <= 500:
            snapped = True
            future = shape[here_i:]
            # Earliest fix that genuinely sits on this alignment marks the
            # start of the run; anything further off is noise or another leg.
            start_i = here_i
            for la, lo in fixes:
                i, m = nearest(la, lo)
                if m <= 250 and i < start_i:
                    start_i = i
            past = shape[start_i:here_i + 1]

    if not past:
        # Fallback: drop fixes that barely moved, so noise does not zigzag.
        cleaned = []
        for pt in fixes:
            if not cleaned or haversine_m(cleaned[-1][0], cleaned[-1][1], pt[0], pt[1]) > 15:
                cleaned.append(pt)
        past = [[a, b] for a, b in cleaned]

    return {
        "id": v["vehicle_id"], "line": v["line_name"],
        "direction": v["direction"], "destination": v["destination_name"],
        "past": past, "future": future,
        "on_route": snapped, "fixes": len(fixes),
    }


def transit_overview(hours=24):
    from . import config
    now = int(time.time())
    since = now - hours * 3600
    cutoff = now - config.TRANSIT_MAX_AGE_S

    live = db.scalar("SELECT COUNT(*) FROM transit_vehicles WHERE last_seen > ?",
                     (cutoff,), default=0)
    routes = db.scalar(
        "SELECT COUNT(DISTINCT route_id) FROM transit_vehicles "
        "WHERE last_seen > ? AND route_id IS NOT NULL", (cutoff,), default=0)
    moving = db.scalar(
        "SELECT COUNT(*) FROM transit_vehicles WHERE last_seen > ? AND speed_kmh > 3",
        (cutoff,), default=0)
    avg_speed = db.scalar(
        "SELECT AVG(speed_kmh) FROM transit_vehicles WHERE last_seen > ? "
        "AND speed_kmh IS NOT NULL AND speed_kmh > 3", (cutoff,))
    by_operator = db.rows(
        "SELECT COALESCE(operator,'Unknown') AS operator, COUNT(*) AS n "
        "FROM transit_vehicles WHERE last_seen > ? GROUP BY operator "
        "ORDER BY n DESC LIMIT 10", (cutoff,))
    samples = db.rows(
        "SELECT ts, active, moving, operators, routes, mean_speed_kmh "
        "FROM transit_samples WHERE ts >= ? ORDER BY ts", (since,))
    speeds = [r["speed_kmh"] for r in db.rows(
        "SELECT speed_kmh FROM transit_positions WHERE ts >= ? AND speed_kmh IS NOT NULL",
        (since,))]
    return {
        "live": live,
        "moving": moving,
        "routes": routes,
        "operators": len(by_operator),
        "avg_speed_kmh": round(avg_speed, 1) if avg_speed else None,
        "by_operator": by_operator,
        "samples": samples,
        "speed_hist": _hist(speeds, [0, 5, 10, 15, 20, 25, 30, 40]),
        "stations": db.scalar(
            "SELECT COUNT(*) FROM osm_features WHERE kind IN ('rail_station','rail_halt')",
            default=0),
    }


# --- rail ----------------------------------------------------------------------

def rail_stations():
    """Stations with a live-board count, for the map."""
    now = int(time.time())
    return db.rows(
        "SELECT s.code, s.name, s.lat, s.lon, "
        "  COUNT(v.uid) AS services, "
        "  ROUND(AVG(CASE WHEN v.is_cancelled=0 THEN v.delay_min END), 1) AS avg_delay, "
        "  SUM(v.is_cancelled) AS cancelled "
        "FROM rail_stations s "
        "LEFT JOIN rail_services v ON v.station_code = s.code "
        "  AND v.scheduled_ts > ? AND v.scheduled_ts < ? "
        "GROUP BY s.code ORDER BY s.name", (now - 1800, now + 7200))


def rail_board(code, limit=40):
    """One station's live departure board."""
    now = int(time.time())
    station = db.one("SELECT code, name, lat, lon FROM rail_stations WHERE code=?", (code,))
    if not station:
        return None
    services = db.rows(
        "SELECT uid, headcode, operator, operator_code, origin, destination, "
        "scheduled_ts, forecast_ts, delay_min, is_cancelled, platform, coaches, leg, seen_ts "
        "FROM rail_services WHERE station_code=? AND scheduled_ts > ? "
        "ORDER BY COALESCE(forecast_ts, scheduled_ts) LIMIT ?",
        (code, now - 3600, limit))
    return {"station": station, "services": services, "now": now}


def rail_overview(hours=24):
    now = int(time.time())
    since = now - hours * 3600
    rows = db.rows(
        "SELECT operator, delay_min, is_cancelled FROM rail_services WHERE seen_ts >= ?",
        (since,))
    delays = [r["delay_min"] for r in rows
              if r["delay_min"] is not None and not r["is_cancelled"]]
    cancelled = sum(1 for r in rows if r["is_cancelled"])

    by_op = {}
    for r in rows:
        op = r["operator"] or "Unknown"
        slot = by_op.setdefault(op, {"operator": op, "n": 0, "delay_sum": 0,
                                     "delay_n": 0, "cancelled": 0})
        slot["n"] += 1
        slot["cancelled"] += 1 if r["is_cancelled"] else 0
        if r["delay_min"] is not None and not r["is_cancelled"]:
            slot["delay_sum"] += r["delay_min"]
            slot["delay_n"] += 1
    operators = []
    for slot in by_op.values():
        operators.append({
            "operator": slot["operator"], "n": slot["n"],
            "cancelled": slot["cancelled"],
            "avg_delay": round(slot["delay_sum"] / slot["delay_n"], 1) if slot["delay_n"] else None,
        })
    operators.sort(key=lambda d: -d["n"])

    # Delay buckets, including early running, which a plain histogram hides.
    buckets = [0] * 7
    for d in delays:
        if d < 0:
            buckets[0] += 1
        elif d == 0:
            buckets[1] += 1
        elif d <= 2:
            buckets[2] += 1
        elif d <= 5:
            buckets[3] += 1
        elif d <= 10:
            buckets[4] += 1
        elif d <= 30:
            buckets[5] += 1
        else:
            buckets[6] += 1

    return {
        "enabled": bool(db.scalar("SELECT COUNT(*) FROM rail_stations", default=0)),
        "stations": db.scalar("SELECT COUNT(*) FROM rail_stations", default=0),
        "services": len(rows),
        "cancelled": cancelled,
        "mean_delay_min": round(sum(delays) / len(delays), 1) if delays else None,
        "on_time_pct": round(100.0 * sum(1 for d in delays if d <= 5) / len(delays), 1) if delays else None,
        "worst": db.rows(
            "SELECT headcode, operator, origin, destination, delay_min, station_code "
            "FROM rail_services WHERE seen_ts >= ? AND delay_min IS NOT NULL "
            "ORDER BY delay_min DESC LIMIT 8", (since,)),
        "by_operator": operators[:8],
        "delay_hist": buckets,
        "samples": db.rows(
            "SELECT ts, services, cancelled, mean_delay_min, on_time_pct "
            "FROM rail_samples WHERE ts >= ? ORDER BY ts", (since,)),
    }


def trains_live(max_age_s=1800):
    """Estimated train positions. Derived from timings — see hush/rail.py."""
    now = int(time.time())
    out = []
    for r in db.rows(
        "SELECT * FROM train_positions WHERE computed_ts > ? ORDER BY headcode",
        (now - max_age_s,)
    ):
        out.append({
            "uid": r["uid"],
            "code": r["headcode"],
            "op": r["operator"],
            "from": r["from_name"], "to": r["to_name"],
            "origin": r["origin"], "destination": r["destination"],
            "lat": round(r["lat"], 6), "lon": round(r["lon"], 6),
            "brg": round(r["bearing"]) if r["bearing"] is not None else None,
            "progress": round(r["progress"] or 0, 3),
            "delay": r["delay_min"],
            "state": r["state"],
            "basis": r["basis"],
            "on_track": r["snapped_m"] is not None,
            "age": now - r["computed_ts"],
            "leg_start": r["leg_start_ts"], "leg_end": r["leg_end_ts"],
            # A decimated version of the leg the train is on. The client walks
            # this by clock time between refreshes, so trains glide instead of
            # jumping once a poll — and it stays consistent with the server,
            # because both advance the same time-based model.
            "leg": _leg_shape(r),
        })
    return out


def _leg_shape(row, target=40):
    """The current leg only, thinned to a handful of points."""
    try:
        past = json.loads(row["path_past"] or "[]")
        future = json.loads(row["path_future"] or "[]")
    except (TypeError, ValueError):
        return []
    # The leg spans the tail of past and the head of future.
    tail = past[-target:] if past else []
    head = future[:target] if future else []
    pts = tail + head
    if len(pts) <= target:
        return pts
    step = max(1, len(pts) // target)
    thinned = pts[::step]
    if thinned[-1] != pts[-1]:
        thinned.append(pts[-1])
    return thinned


def train_path(uid):
    """One train's journey geometry, split at its current position."""
    r = db.one("SELECT uid, headcode, operator, origin, destination, path_past, "
               "path_future, calls, progress, state FROM train_positions WHERE uid=?",
               (uid,))
    if not r:
        return None
    return {
        "uid": r["uid"], "code": r["headcode"], "op": r["operator"],
        "origin": r["origin"], "destination": r["destination"],
        "state": r["state"], "progress": r["progress"],
        "past": json.loads(r["path_past"] or "[]"),
        "future": json.loads(r["path_future"] or "[]"),
        "calls": json.loads(r["calls"] or "[]"),
    }


def bus_insights(hours=24):
    """Route-level statistics: congestion, frequency and bunching."""
    from . import config
    now = int(time.time())
    since = now - hours * 3600
    cutoff = now - config.TRANSIT_MAX_AGE_S

    # Average speed per route. A consistently slow route is a congested one.
    by_route = db.rows(
        "SELECT v.line_name AS line, COUNT(*) AS fixes, "
        "  AVG(p.speed_kmh) AS avg_kmh, MAX(p.speed_kmh) AS max_kmh "
        "FROM transit_positions p JOIN transit_vehicles v USING (vehicle_id) "
        "WHERE p.ts >= ? AND p.speed_kmh IS NOT NULL AND p.speed_kmh > 1 "
        "  AND v.line_name IS NOT NULL "
        "GROUP BY v.line_name HAVING fixes >= 3 ORDER BY avg_kmh ASC", (since,))
    for r in by_route:
        r["avg_kmh"] = round(r["avg_kmh"], 1)
        r["max_kmh"] = round(r["max_kmh"], 1)

    # Speed by hour of day: the shape of the city's congestion.
    hod = [None] * 24
    for r in db.rows(
        "SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS h, "
        "AVG(speed_kmh) AS s FROM transit_positions "
        "WHERE ts >= ? AND speed_kmh > 1 GROUP BY h", (since,)
    ):
        hod[r["h"]] = round(r["s"], 1)

    # Bunching: two buses on the same route sitting almost on top of each
    # other, which means a gap somewhere else. A classic sign of a route
    # struggling, and visible from positions alone.
    live = db.rows(
        "SELECT vehicle_id, line_name, lat, lon FROM transit_vehicles "
        "WHERE last_seen > ? AND line_name IS NOT NULL", (cutoff,))
    groups = {}
    for v in live:
        groups.setdefault(v["line_name"], []).append(v)
    bunched = []
    for line, vs in groups.items():
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                d = haversine_m(vs[i]["lat"], vs[i]["lon"], vs[j]["lat"], vs[j]["lon"])
                if d < 300:
                    bunched.append({"line": line, "metres": round(d),
                                    "lat": vs[i]["lat"], "lon": vs[i]["lon"]})
    bunched.sort(key=lambda b: b["metres"])

    fleet_km = db.scalar("SELECT SUM(distance_m) FROM transit_vehicles", default=0) or 0
    stopped = db.scalar(
        "SELECT COUNT(*) FROM transit_vehicles WHERE last_seen > ? "
        "AND (speed_kmh IS NULL OR speed_kmh <= 3)", (cutoff,), default=0)
    active = db.scalar(
        "SELECT COUNT(*) FROM transit_vehicles WHERE last_seen > ?", (cutoff,), default=0)

    return {
        "slowest_routes": by_route[:8],
        "fastest_routes": list(reversed(by_route))[:8],
        "busiest_routes": db.rows(
            "SELECT line_name AS line, COUNT(*) AS buses FROM transit_vehicles "
            "WHERE last_seen > ? AND line_name IS NOT NULL "
            "GROUP BY line_name ORDER BY buses DESC LIMIT 8", (cutoff,)),
        "speed_by_hour": hod,
        "bunched": bunched[:10],
        "bunched_count": len(bunched),
        "fleet_km": round(fleet_km / 1000.0, 1),
        "stopped": stopped,
        "stopped_pct": round(100.0 * stopped / active, 1) if active else 0,
        "routes_with_shapes": db.scalar(
            "SELECT COUNT(DISTINCT line_name) FROM bus_routes", default=0),
    }


def rail_insights(hours=24):
    """Station and timing patterns across the rail services observed."""
    now = int(time.time())
    since = now - hours * 3600
    busiest = db.rows(
        "SELECT s.name, v.station_code AS code, COUNT(*) AS services, "
        "  ROUND(AVG(CASE WHEN v.is_cancelled=0 THEN v.delay_min END), 1) AS avg_delay "
        "FROM rail_services v LEFT JOIN rail_stations s ON s.code = v.station_code "
        "WHERE v.seen_ts >= ? GROUP BY v.station_code "
        "ORDER BY services DESC LIMIT 10", (since,))
    hod = [None] * 24
    for r in db.rows(
        "SELECT CAST(strftime('%H', scheduled_ts, 'unixepoch', 'localtime') AS INTEGER) AS h, "
        "AVG(delay_min) AS d FROM rail_services "
        "WHERE seen_ts >= ? AND delay_min IS NOT NULL AND is_cancelled = 0 "
        "GROUP BY h", (since,)
    ):
        hod[r["h"]] = round(r["d"], 1)
    return {
        "busiest_stations": busiest,
        "delay_by_hour": hod,
        "destinations": db.rows(
            "SELECT destination, COUNT(*) AS n FROM rail_services "
            "WHERE seen_ts >= ? AND destination IS NOT NULL "
            "GROUP BY destination ORDER BY n DESC LIMIT 8", (since,)),
    }


def osm_features(kinds=None):
    sql = "SELECT osm_id, kind, name, lat, lon FROM osm_features"
    args = ()
    if kinds:
        marks = ",".join("?" * len(kinds))
        sql += f" WHERE kind IN ({marks})"
        args = tuple(kinds)
    return db.rows(sql, args)


def hotspots(hours=24, kind="pickup", cell=0.002, limit=300):
    """Grid-cluster rental events. cell=0.002 deg is roughly 220m."""
    since = int(time.time()) - hours * 3600
    grid = {}
    for r in db.rows(
        "SELECT lat, lon FROM events WHERE kind=? AND ts >= ? "
        "AND lat IS NOT NULL", (kind, since)
    ):
        key = (round(r["lat"] / cell), round(r["lon"] / cell))
        g = grid.setdefault(key, [0, 0.0, 0.0])
        g[0] += 1
        g[1] += r["lat"]
        g[2] += r["lon"]
    out = [{"lat": round(v[1] / v[0], 6), "lon": round(v[2] / v[0], 6), "n": v[0]}
           for v in grid.values()]
    out.sort(key=lambda d: -d["n"])
    return out[:limit]


def balance(hours=24, cell=0.004, limit=300):
    """Net gain/loss of vehicles per area: where rebalancing is needed.

    Positive means more rentals ended there than started -- vehicles pile up.
    Negative means the area drains.
    """
    since = int(time.time()) - hours * 3600
    grid = {}
    for r in db.rows(
        "SELECT lat, lon, kind FROM events WHERE ts >= ? AND lat IS NOT NULL", (since,)
    ):
        key = (round(r["lat"] / cell), round(r["lon"] / cell))
        g = grid.setdefault(key, {"in": 0, "out": 0, "la": 0.0, "lo": 0.0, "n": 0})
        g["in" if r["kind"] == "dropoff" else "out"] += 1
        g["la"] += r["lat"]
        g["lo"] += r["lon"]
        g["n"] += 1
    out = [{"lat": round(v["la"] / v["n"], 6), "lon": round(v["lo"] / v["n"], 6),
            "net": v["in"] - v["out"], "in": v["in"], "out": v["out"]}
           for v in grid.values()]
    out.sort(key=lambda d: -abs(d["net"]))
    return out[:limit]


def recent_events(hours=24, limit=100, kind=None):
    since = int(time.time()) - hours * 3600
    sql = ("SELECT id, ts, kind, bike_id, vehicle_type_id, lat, lon, fuel, dwell_s "
           "FROM events WHERE ts >= ?")
    args = [since]
    if kind in ("pickup", "dropoff"):
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return db.rows(sql, tuple(args))


def leaderboard(hours=24, limit=25):
    """Vehicles worth an operator's attention."""
    now = int(time.time())
    stranded = db.rows(
        "SELECT bike_id, vehicle_type_id, fuel, lat, lon, parked_since, "
        "? - parked_since AS idle_s FROM vehicles "
        "WHERE present=1 AND parked_since IS NOT NULL "
        "ORDER BY parked_since ASC LIMIT ?", (now, limit))
    flat = db.rows(
        "SELECT bike_id, vehicle_type_id, fuel, lat, lon, parked_since, "
        "? - parked_since AS idle_s FROM vehicles "
        "WHERE present=1 AND fuel IS NOT NULL "
        "ORDER BY fuel ASC LIMIT ?", (now, limit))
    since = now - hours * 3600
    quickest = db.rows(
        "SELECT bike_id, vehicle_type_id, lat, lon, fuel, dwell_s, ts FROM events "
        "WHERE kind='pickup' AND ts >= ? AND dwell_s IS NOT NULL "
        "ORDER BY dwell_s ASC LIMIT ?", (since, limit))
    return {"stranded": stranded, "flat": flat, "quickest": quickest}


def static_feed(name):
    r = db.one("SELECT payload FROM static_feeds WHERE name=?", (name,))
    return json.loads(r["payload"]) if r else None
