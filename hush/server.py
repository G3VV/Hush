"""HTTP API + static file server for the Hush dashboard.

Runs the collector on a background thread by default so a single command gives
you a working dashboard that keeps filling in history while it is open.
"""

import argparse
import json
import mimetypes
import os
import posixpath
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse
from urllib.parse import urlparse, parse_qs

from . import analytics, collector, config, db

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


def _int(qs, key, default=None, lo=None, hi=None):
    try:
        v = int(qs.get(key, [default])[0])
    except (TypeError, ValueError):
        return default
    if lo is not None:
        v = max(lo, v)
    if hi is not None:
        v = min(hi, v)
    return v


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "Hush"

    def log_message(self, fmt, *args):
        pass  # the collector's output is the interesting log

    # -- plumbing --------------------------------------------------------------
    def _send(self, code, body, ctype="application/json", cache=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":")), "application/json",
                   "no-store")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = posixpath.normpath(parsed.path)
        qs = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self._api(path, qs)
            return self._static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    # -- api -------------------------------------------------------------------
    def _api(self, path, qs):
        hours = _int(qs, "hours", 24, 1, 24 * 30)

        if path == "/api/health":
            last = db.one("SELECT * FROM polls ORDER BY ts DESC LIMIT 1")
            return self._json({
                "ok": True,
                "city": config.CITY,
                "now": int(time.time()),
                "last_poll": last,
                "polls": db.scalar("SELECT COUNT(*) FROM polls WHERE ok=1", default=0),
                "vehicles": db.scalar("SELECT COUNT(*) FROM vehicles WHERE present=1", default=0),
                "rentals": db.scalar(
                    "SELECT COUNT(*) FROM events WHERE kind='pickup'", default=0),
                "poll_interval_s": config.POLL_INTERVAL_S,
                "center": config.DEFAULT_CENTER,
                "zoom": config.DEFAULT_ZOOM,
            })

        if path == "/api/live":
            return self._json({
                "ts": int(time.time()),
                "vehicles": analytics.live_vehicles(
                    vtype=qs.get("type", [None])[0],
                    min_fuel=_int(qs, "min_fuel", None, 0, 100),
                    max_fuel=_int(qs, "max_fuel", None, 0, 100),
                    status=qs.get("status", [None])[0],
                ),
            })

        if path.startswith("/api/vehicle/"):
            bid = path.rsplit("/", 1)[-1]
            detail = analytics.vehicle_detail(bid)
            if not detail:
                return self._json({"error": "unknown vehicle"}, 404)
            return self._json(detail)

        if path == "/api/stats":
            return self._json(analytics.overview(hours))

        if path == "/api/hotspots":
            return self._json({
                "pickups": analytics.hotspots(hours, "pickup"),
                "dropoffs": analytics.hotspots(hours, "dropoff"),
            })

        if path == "/api/transit":
            return self._json({
                "ts": int(time.time()),
                "vehicles": analytics.transit_live(
                    operator=qs.get("operator", [None])[0]),
            })

        if path.startswith("/api/transit/vehicle/"):
            vid = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            detail = analytics.transit_vehicle(vid)
            if not detail:
                return self._json({"error": "unknown vehicle"}, 404)
            return self._json(detail)

        if path == "/api/rail":
            return self._json({"stations": analytics.rail_stations()})

        if path.startswith("/api/rail/station/"):
            code = urllib.parse.unquote(path.rsplit("/", 1)[-1])
            board = analytics.rail_board(code)
            if not board:
                return self._json({"error": "unknown station"}, 404)
            return self._json(board)

        if path == "/api/infrastructure":
            kinds = qs.get("kind")
            return self._json({"features": analytics.osm_features(kinds)})

        if path == "/api/balance":
            return self._json({"cells": analytics.balance(hours)})

        if path == "/api/events":
            return self._json({"events": analytics.recent_events(
                hours, _int(qs, "limit", 100, 1, 1000),
                qs.get("kind", [None])[0])})

        if path == "/api/leaderboard":
            return self._json(analytics.leaderboard(hours))

        if path == "/api/zones":
            feed = analytics.static_feed("geofencing_zones")
            if not feed:
                return self._json({"error": "not collected yet"}, 503)
            return self._json(feed["data"]["geofencing_zones"])

        if path == "/api/stations":
            feed = analytics.static_feed("station_information")
            if not feed:
                return self._json({"error": "not collected yet"}, 503)
            return self._json({"stations": feed["data"]["stations"]})

        if path == "/api/pricing":
            feed = analytics.static_feed("system_pricing_plans")
            return self._json(feed["data"] if feed else {"plans": []})

        return self._json({"error": "no such endpoint"}, 404)

    # -- static ----------------------------------------------------------------
    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        target = os.path.normpath(os.path.join(WEB_DIR, path.lstrip("/")))
        if not target.startswith(WEB_DIR) or not os.path.isfile(target):
            return self._send(404, "not found", "text/plain")
        ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
        with open(target, "rb") as fh:
            self._send(200, fh.read(), ctype, "no-cache")


def start_collector_thread():
    def loop():
        # Built inside the thread: a SQLite connection belongs to the thread
        # that opened it, and Collector opens one in __init__.
        col = collector.Collector()
        col.refresh_static(force=False)
        col.run_forever()

    t = threading.Thread(target=loop, name="collector", daemon=True)
    t.start()
    return t


def main():
    ap = argparse.ArgumentParser(description="Hush - Dott fleet map & analytics")
    ap.add_argument("--host", default=config.SERVER_HOST)
    ap.add_argument("--port", type=int, default=config.SERVER_PORT)
    ap.add_argument("--no-collector", action="store_true",
                    help="serve only; do not poll the Dott feed")
    args = ap.parse_args()

    db.init()
    if not args.no_collector:
        start_collector_thread()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"[server] http://{args.host}:{args.port}  (city: {config.CITY})", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] stopping", flush=True)
        srv.shutdown()


if __name__ == "__main__":
    main()
