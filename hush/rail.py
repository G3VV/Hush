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
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config, db

BASE = "https://data.rtt.io"

_token_lock = threading.Lock()
_access = {"token": None, "expires": 0}


class RailUnavailable(Exception):
    """Raised when rail data cannot be fetched (no token, auth failure)."""


def _request(path, token, timeout=30):
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

    osm = db.rows(
        "SELECT name, lat, lon FROM osm_features "
        "WHERE kind IN ('rail_station','rail_halt') AND name IS NOT NULL")
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

    stations = db.rows("SELECT code FROM rail_stations ORDER BY code")
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
        # 30 requests/minute is the documented ceiling; stay well inside it.
        time.sleep(0.4)

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
