"""Shared scooters and bikes: polls the Dott GBFS feed and reconstructs history.

This module covers the micromobility side only. Buses live in transit.py and
trains in rail.py.

The GBFS `free_bike_status` feed lists only vehicles that are parked and
rentable right now. There is no trip history in the API. What we can observe:

    * a vehicle is listed at a location  -> it is parked there
    * it disappears from the feed        -> someone rented it (or ops took it)
    * it reappears somewhere else        -> that ride ended there

Chaining those events together gives each vehicle a history of stays and the
movements between them. Everything in `trips` is inferred by this module, not
reported by Dott.
"""

import json
import math
import time
import urllib.error
import urllib.request

from . import config, db


# --- helpers -------------------------------------------------------------------

def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance in metres."""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def fetch_json(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=config.HTTP_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8"))


def classify(distance_m, duration_s, fuel_delta):
    """Label a movement. Returns (kind, confidence).

    We cannot see who moved a vehicle, only the before/after. These rules
    separate plausible customer rides from operational moves.
    """
    if distance_m < config.DRIFT_M:
        # Same spot within GPS scatter: the vehicle never actually went anywhere.
        return "drift", 0.9

    if fuel_delta is not None and fuel_delta >= config.SERVICE_FUEL_GAIN:
        # Battery went *up*: swapped or charged. That is a depot, not a customer.
        return "service", 0.85

    kmh = (distance_m / 1000.0) / (duration_s / 3600.0) if duration_s > 0 else 999.0
    if kmh > config.MAX_RIDE_KMH:
        # Faster than these vehicles can physically go: carried in a van.
        return "relocation", 0.8

    if duration_s > config.MAX_RIDE_S:
        # Gone for hours. More likely collected overnight than one long ride.
        return "relocation", 0.5

    # Straight-line distance understates real riding, so confidence rises with
    # a plausible average speed and falls for suspiciously slow long hauls.
    conf = 0.85 if 2.0 <= kmh <= 25.0 else 0.6
    return "trip", conf


# --- collector -----------------------------------------------------------------

class Collector:
    def __init__(self):
        self.conn = db.init()
        self._static_checked_at = 0
        # Vehicles already parked when we started were parked for an unknown
        # time before that, so their dwell would be an undercount.
        self._first_poll_ts = db.scalar("SELECT MIN(ts) FROM polls WHERE ok=1")

    # -- static feeds ----------------------------------------------------------
    def refresh_static(self, force=False):
        now = int(time.time())
        if not force and now - self._static_checked_at < 60:
            return
        self._static_checked_at = now
        for name in ("vehicle_types", "system_pricing_plans", "geofencing_zones",
                     "station_information", "system_information"):
            row = db.one("SELECT fetched_at FROM static_feeds WHERE name=?", (name,))
            if row and not force and now - row["fetched_at"] < config.STATIC_REFRESH_S:
                continue
            try:
                payload = fetch_json(config.FEEDS[name])
            except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
                print(f"[collector] static feed {name} failed: {exc}", flush=True)
                continue
            self.conn.execute(
                "INSERT INTO static_feeds(name, fetched_at, payload) VALUES(?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "payload=excluded.payload",
                (name, now, json.dumps(payload)),
            )
            self.conn.commit()
            print(f"[collector] cached {name}", flush=True)

    # -- main poll -------------------------------------------------------------
    def poll(self):
        started = time.time()
        now = int(started)
        try:
            feed = fetch_json(config.FEEDS["free_bike_status"])
        except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
            self.conn.execute(
                "INSERT INTO polls(ts, ok, error, duration_ms) VALUES(?,0,?,?)",
                (now, str(exc), int((time.time() - started) * 1000)),
            )
            self.conn.commit()
            print(f"[collector] poll failed: {exc}", flush=True)
            return {"ok": False, "error": str(exc)}

        bikes = feed.get("data", {}).get("bikes", []) or []
        feed_ts = feed.get("last_updated")

        # Previous state, keyed by id.
        prev = {r["bike_id"]: r for r in db.rows(
            "SELECT bike_id, lat, lon, fuel, present, parked_since, last_seen, "
            "vehicle_type_id FROM vehicles"
        )}

        # On the very first run every vehicle looks new; that is not 3000 rides
        # ending at once, so events are only recorded once we have a baseline.
        have_baseline = bool(prev)

        seen = set()
        appeared = vanished = trips_found = 0
        veh_rows, trip_rows, event_rows = [], [], []
        open_park_updates, new_parkings = [], []

        n_scooter = n_bike = n_disabled = n_reserved = n_low = 0
        fuel_total = 0.0

        for b in bikes:
            bid = b.get("bike_id")
            if not bid or b.get("lat") is None or b.get("lon") is None:
                continue
            seen.add(bid)
            lat, lon = float(b["lat"]), float(b["lon"])
            fuel = b.get("current_fuel_percent")
            vtype = b.get("vehicle_type_id") or "unknown"
            disabled = 1 if b.get("is_disabled") else 0
            reserved = 1 if b.get("is_reserved") else 0

            if vtype == "dott_bicycle":
                n_bike += 1
            else:
                n_scooter += 1
            n_disabled += disabled
            n_reserved += reserved
            if fuel is not None:
                fuel_total += float(fuel)
                if float(fuel) < 0.2:
                    n_low += 1

            p = prev.get(bid)
            if p is None:
                # A brand-new id. Because ids rotate after every rental, this is
                # almost always a rental ENDING here (occasionally a fresh
                # deployment from the depot).
                appeared += 1
                if have_baseline:
                    event_rows.append((now, "dropoff", bid, vtype, lat, lon, fuel, None))
                veh_rows.append((bid, vtype, b.get("pricing_plan_id"), now, now, 1,
                                 lat, lon, fuel, b.get("current_range_meters"),
                                 disabled, reserved, b.get("last_reported"), now))
                new_parkings.append((bid, lat, lon, now, fuel))
                continue

            moved = haversine_m(p["lat"], p["lon"], lat, lon) if p["lat"] is not None else 0.0
            was_present = bool(p["present"])

            if not was_present:
                # It came back. The gap between last_seen and now is the ride.
                appeared += 1
                trip = self._build_trip(bid, vtype, p, lat, lon, fuel, now)
                if trip:
                    trip_rows.append(trip)
                    trips_found += 1
                open_park_updates.append((p["last_seen"], p["fuel"], bid))
                new_parkings.append((bid, lat, lon, now, fuel))
                parked_since = now
            elif moved > config.DRIFT_M:
                # Moved while still listed as available -- ops shuffling it, or a
                # ride so short it fell between two polls.
                trip = self._build_trip(bid, vtype, p, lat, lon, fuel, now,
                                        start_ts=p["last_seen"])
                if trip:
                    trip_rows.append(trip)
                    trips_found += 1
                open_park_updates.append((p["last_seen"], p["fuel"], bid))
                new_parkings.append((bid, lat, lon, now, fuel))
                parked_since = now
            else:
                # Sitting still. Extend the current stay.
                parked_since = p["parked_since"] or now

            veh_rows.append((bid, vtype, b.get("pricing_plan_id"),
                             p["last_seen"] if p else now, now, 1, lat, lon, fuel,
                             b.get("current_range_meters"), disabled, reserved,
                             b.get("last_reported"), parked_since))

        # Anything present last time but missing now has just been taken: a
        # rental starting, or ops collecting it.
        gone = [bid for bid, p in prev.items() if p["present"] and bid not in seen]
        vanished = len(gone)
        for bid in gone:
            p = prev[bid]
            parked = p["parked_since"]
            # Only report dwell when we actually watched the vehicle arrive.
            observed_arrival = (
                parked is not None and
                (self._first_poll_ts is None or parked > self._first_poll_ts)
            )
            event_rows.append((now, "pickup", bid, p["vehicle_type_id"], p["lat"],
                               p["lon"], p["fuel"],
                               now - parked if observed_arrival else None))

        cur = self.conn
        cur.execute("BEGIN")
        try:
            cur.executemany(
                "INSERT INTO vehicles(bike_id, vehicle_type_id, pricing_plan_id, "
                "first_seen, last_seen, present, lat, lon, fuel, range_m, "
                "is_disabled, is_reserved, last_reported, parked_since) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bike_id) DO UPDATE SET "
                " vehicle_type_id=excluded.vehicle_type_id,"
                " pricing_plan_id=excluded.pricing_plan_id,"
                " last_seen=excluded.last_seen, present=1,"
                " lat=excluded.lat, lon=excluded.lon, fuel=excluded.fuel,"
                " range_m=excluded.range_m, is_disabled=excluded.is_disabled,"
                " is_reserved=excluded.is_reserved,"
                " last_reported=excluded.last_reported,"
                " parked_since=excluded.parked_since",
                veh_rows,
            )
            if gone:
                cur.executemany("UPDATE vehicles SET present=0 WHERE bike_id=?",
                                [(g,) for g in gone])
                # Close their stay at the last moment we actually saw them.
                cur.executemany(
                    "UPDATE parkings SET is_open=0, end_ts=COALESCE(end_ts,?), "
                    "end_fuel=COALESCE(end_fuel, start_fuel) "
                    "WHERE bike_id=? AND is_open=1",
                    [(now, g) for g in gone],
                )
            if open_park_updates:
                cur.executemany(
                    "UPDATE parkings SET is_open=0, end_ts=?, end_fuel=COALESCE(?, end_fuel) "
                    "WHERE bike_id=? AND is_open=1",
                    open_park_updates,
                )
            if new_parkings:
                cur.executemany(
                    "INSERT INTO parkings(bike_id, lat, lon, start_ts, start_fuel, is_open) "
                    "VALUES(?,?,?,?,?,1)", new_parkings,
                )
            if trip_rows:
                cur.executemany(
                    "INSERT INTO trips(bike_id, vehicle_type_id, start_lat, start_lon, "
                    "end_lat, end_lon, start_ts, end_ts, duration_s, distance_m, "
                    "fuel_start, fuel_end, fuel_delta, kind, confidence) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", trip_rows,
                )
                cur.executemany(
                    "UPDATE vehicles SET trip_count=trip_count+1, "
                    "trip_distance_m=trip_distance_m+? WHERE bike_id=?",
                    [(t[9], t[0]) for t in trip_rows if t[13] == "trip"],
                )

            if event_rows:
                cur.executemany(
                    "INSERT INTO events(ts, kind, bike_id, vehicle_type_id, lat, lon, "
                    "fuel, dwell_s) VALUES(?,?,?,?,?,?,?,?)", event_rows,
                )

            available = len(seen)
            # "Riding" = absent but seen recently. Vehicles absent for hours are
            # off the street for maintenance and would inflate the estimate.
            riding = cur.execute(
                "SELECT COUNT(*) FROM vehicles WHERE present=0 AND last_seen > ?",
                (now - 2 * 3600,)).fetchone()[0]
            cur.execute(
                "INSERT OR REPLACE INTO fleet_samples(ts, available, scooters, "
                "bicycles, disabled, reserved, avg_fuel, low_battery, in_use) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (now, available, n_scooter, n_bike, n_disabled, n_reserved,
                 (fuel_total / available) if available else None, n_low, riding),
            )
            cur.execute(
                "INSERT INTO polls(ts, feed_last_updated, vehicle_count, appeared, "
                "vanished, trips_found, duration_ms, ok) VALUES(?,?,?,?,?,?,?,1)",
                (now, feed_ts, available, appeared, vanished, trips_found,
                 int((time.time() - started) * 1000)),
            )
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise

        print(f"[collector] {time.strftime('%H:%M:%S')} "
              f"{len(seen)} available  +{appeared} -{vanished}  "
              f"{trips_found} movements  {int((time.time()-started)*1000)}ms", flush=True)
        return {"ok": True, "available": len(seen), "appeared": appeared,
                "vanished": vanished, "trips": trips_found}

    def _build_trip(self, bid, vtype, prev_row, lat, lon, fuel, now, start_ts=None):
        if prev_row["lat"] is None:
            return None
        dist = haversine_m(prev_row["lat"], prev_row["lon"], lat, lon)
        start_ts = start_ts or prev_row["last_seen"]
        duration = max(0, now - start_ts)
        f0 = prev_row["fuel"]
        delta = (fuel - f0) if (fuel is not None and f0 is not None) else None
        kind, conf = classify(dist, duration, delta)
        if kind == "drift":
            return None
        return (bid, vtype, prev_row["lat"], prev_row["lon"], lat, lon,
                start_ts, now, duration, dist, f0, fuel, delta, kind, conf)

    def prune(self, keep_days=None):
        """Drop long-retired ids.

        Every rental retires an id permanently, so the vehicles table would grow
        by thousands a day if nothing cleared it out. Events, polls and fleet
        samples are the historical record and are kept.
        """
        keep_days = keep_days or config.RETAIN_DAYS
        cutoff = int(time.time()) - keep_days * 86400
        cur = self.conn
        stale = [r["bike_id"] for r in db.rows(
            "SELECT bike_id FROM vehicles WHERE present=0 AND last_seen < ?", (cutoff,))]
        if not stale:
            return 0
        cur.execute("BEGIN")
        try:
            cur.executemany("DELETE FROM parkings WHERE bike_id=?", [(s,) for s in stale])
            cur.executemany("DELETE FROM trips WHERE bike_id=?", [(s,) for s in stale])
            cur.executemany("DELETE FROM vehicles WHERE bike_id=?", [(s,) for s in stale])
            cur.execute("COMMIT")
        except Exception:
            cur.execute("ROLLBACK")
            raise
        print(f"[collector] pruned {len(stale)} retired ids", flush=True)
        return len(stale)

    # -- loop ------------------------------------------------------------------
    def run_forever(self, interval=None):
        interval = interval or config.POLL_INTERVAL_S
        print(f"[collector] polling {config.CITY} every {interval}s", flush=True)
        last_prune = 0
        last_scooter = 0
        last_transit = 0
        last_transit_wide = 0
        last_transit_slow = 0
        last_rail = 0
        while True:
            cycle = time.time()
            try:
                self.refresh_static()
                if cycle - last_scooter >= interval:
                    last_scooter = cycle
                    self.poll()
                # Public transport runs on its own, slower cadence: the BODS
                # feed is a couple of megabytes and covers the whole country.
                if config.TRANSIT_ENABLED:
                    from . import transit
                    fast = bool(config.BODS_API_KEY)
                    # Two feeds, different jobs. SIRI is small and Bristol-only
                    # so it runs often and keeps the moving vehicles current;
                    # the national GTFS-RT file is large but catches vehicles
                    # SIRI reports only rarely, so it runs slowly for coverage.
                    if fast and cycle - last_transit >= config.TRANSIT_FAST_INTERVAL_S:
                        last_transit = cycle
                        transit.poll_siri(self.conn)
                    if cycle - last_transit_wide >= config.TRANSIT_POLL_INTERVAL_S:
                        last_transit_wide = cycle
                        transit.poll(self.conn)
                        if fast:
                            transit.refresh_line_names(self.conn)
                    if cycle - last_transit_slow >= 3600:
                        last_transit_slow = cycle
                        transit.refresh_osm()
                        transit.refresh_route_shapes()
                if (config.RAIL_ENABLED and config.RTT_TOKEN
                        and cycle - last_rail >= config.RAIL_POLL_INTERVAL_S):
                    last_rail = cycle
                    from . import rail
                    rail.poll()
                    if config.TRAIN_POSITIONS:
                        rail.position_trains()
                if cycle - last_prune > 3600:
                    self.prune()
                    if config.TRANSIT_ENABLED:
                        from . import transit
                        transit.prune(self.conn)
                    if config.RAIL_ENABLED and config.RTT_TOKEN:
                        from . import rail
                        rail.prune()
                    last_prune = cycle
            except Exception as exc:  # keep the loop alive across anything
                print(f"[collector] unexpected error: {exc!r}", flush=True)
            # The loop ticks faster than the scooter poll so that transit,
            # which refreshes far more often, is not held back by it.
            tick = min(interval, config.TRANSIT_FAST_INTERVAL_S) if config.TRANSIT_ENABLED else interval
            time.sleep(max(1.0, tick - (time.time() - cycle)))


def main():
    Collector().run_forever()


if __name__ == "__main__":
    main()
