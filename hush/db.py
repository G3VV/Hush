"""SQLite storage for observed fleet state, parkings and inferred trips."""

import os
import sqlite3
import threading

from . import config

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS polls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                INTEGER NOT NULL,
    feed_last_updated INTEGER,
    vehicle_count     INTEGER,
    appeared          INTEGER,
    vanished          INTEGER,
    trips_found       INTEGER,
    duration_ms       INTEGER,
    ok                INTEGER DEFAULT 1,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS polls_ts ON polls(ts);

-- One row per vehicle, updated in place: the live fleet.
CREATE TABLE IF NOT EXISTS vehicles (
    bike_id         TEXT PRIMARY KEY,
    vehicle_type_id TEXT,
    pricing_plan_id TEXT,
    first_seen      INTEGER,
    last_seen       INTEGER,
    present         INTEGER DEFAULT 1,   -- in the most recent feed?
    lat             REAL,
    lon             REAL,
    fuel            REAL,
    range_m         INTEGER,
    is_disabled     INTEGER,
    is_reserved     INTEGER,
    last_reported   INTEGER,
    parked_since    INTEGER,             -- start of the current stay
    trip_count      INTEGER DEFAULT 0,
    trip_distance_m REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS vehicles_present ON vehicles(present);
CREATE INDEX IF NOT EXISTS vehicles_type ON vehicles(vehicle_type_id);

-- A continuous period during which a vehicle sat at one spot, available.
CREATE TABLE IF NOT EXISTS parkings (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    bike_id    TEXT NOT NULL,
    lat        REAL,
    lon        REAL,
    start_ts   INTEGER,
    end_ts     INTEGER,
    start_fuel REAL,
    end_fuel   REAL,
    is_open    INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS parkings_bike ON parkings(bike_id, start_ts DESC);
CREATE INDEX IF NOT EXISTS parkings_open ON parkings(is_open);

-- Movement between two stays. Inferred, never reported by the API.
CREATE TABLE IF NOT EXISTS trips (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bike_id         TEXT NOT NULL,
    vehicle_type_id TEXT,
    start_lat       REAL, start_lon REAL,
    end_lat         REAL, end_lon   REAL,
    start_ts        INTEGER, end_ts INTEGER,
    duration_s      INTEGER,
    distance_m      REAL,
    fuel_start      REAL, fuel_end  REAL, fuel_delta REAL,
    kind            TEXT,      -- trip | relocation | service | drift
    confidence      REAL
);
CREATE INDEX IF NOT EXISTS trips_bike ON trips(bike_id, end_ts DESC);
CREATE INDEX IF NOT EXISTS trips_end ON trips(end_ts DESC);
CREATE INDEX IF NOT EXISTS trips_kind ON trips(kind, end_ts DESC);

-- Cached static feeds (zones, stations, pricing) as raw JSON.
CREATE TABLE IF NOT EXISTS static_feeds (
    name       TEXT PRIMARY KEY,
    fetched_at INTEGER,
    payload    TEXT
);

-- Fleet-wide rollup, one row per poll, for the timeline charts.
CREATE TABLE IF NOT EXISTS fleet_samples (
    ts            INTEGER PRIMARY KEY,
    available     INTEGER,
    scooters      INTEGER,
    bicycles      INTEGER,
    disabled      INTEGER,
    reserved      INTEGER,
    avg_fuel      REAL,
    low_battery   INTEGER,
    in_use        INTEGER    -- absent and recently seen: taken to be riding
);

-- Rental events. Dott rotates bike_id after every rental, so a vehicle that
-- goes out never comes back under the same id: we can see that a rental
-- STARTED here and that one ENDED there, but not which start goes with which
-- end. These are those one-sided observations.
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              INTEGER NOT NULL,
    kind            TEXT NOT NULL,   -- pickup (left the feed) | dropoff (new id appeared)
    bike_id         TEXT,
    vehicle_type_id TEXT,
    lat             REAL,
    lon             REAL,
    fuel            REAL,
    dwell_s         INTEGER          -- pickup only: how long it had been parked
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts DESC);
CREATE INDEX IF NOT EXISTS events_kind ON events(kind, ts DESC);

-- ---------------------------------------------------------------------------
-- Public transport. Unlike the scooters, bus vehicle ids are stable fleet
-- identifiers, so these vehicles can be followed properly and have real tracks.
CREATE TABLE IF NOT EXISTS transit_vehicles (
    vehicle_id   TEXT PRIMARY KEY,
    operator     TEXT,
    mode         TEXT,          -- bus | coach
    route_id     TEXT,
    trip_id      TEXT,
    start_time   TEXT,
    lat          REAL,
    lon          REAL,
    bearing      REAL,
    speed_kmh    REAL,          -- derived between polls; the feed omits speed
    reported_ts  INTEGER,       -- operator's own timestamp
    first_seen   INTEGER,
    last_seen    INTEGER,
    distance_m   REAL DEFAULT 0,
    fixes        INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS transit_last_seen ON transit_vehicles(last_seen DESC);
CREATE INDEX IF NOT EXISTS transit_operator ON transit_vehicles(operator);

CREATE TABLE IF NOT EXISTS transit_positions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_id  TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    lat         REAL, lon REAL,
    bearing     REAL,
    speed_kmh   REAL
);
CREATE INDEX IF NOT EXISTS transit_pos_vehicle ON transit_positions(vehicle_id, ts DESC);
CREATE INDEX IF NOT EXISTS transit_pos_ts ON transit_positions(ts);

CREATE TABLE IF NOT EXISTS transit_samples (
    ts             INTEGER PRIMARY KEY,
    active         INTEGER,
    moving         INTEGER,
    operators      INTEGER,
    routes         INTEGER,
    mean_speed_kmh REAL,
    feed_age_s     INTEGER
);

-- Bus route geometry from TransXChange timetables (BODS). Gives the road the
-- route actually follows, so a vehicle's remaining path can be drawn.
CREATE TABLE IF NOT EXISTS bus_routes (
    line_name  TEXT NOT NULL,
    direction  TEXT NOT NULL,
    points     TEXT,            -- JSON [[lat,lon],...] in travel order
    n_points   INTEGER,
    fetched_at INTEGER,
    PRIMARY KEY (line_name, direction)
);

-- Static infrastructure from OpenStreetMap.
CREATE TABLE IF NOT EXISTS osm_features (
    osm_id     INTEGER PRIMARY KEY,
    kind       TEXT,     -- rail_station | rail_halt | bus_stop | bus_station | ferry_terminal
    name       TEXT,
    lat        REAL,
    lon        REAL,
    fetched_at INTEGER
);
CREATE INDEX IF NOT EXISTS osm_kind ON osm_features(kind);

-- Rail. Realtime Trains gives timings, not positions, so these are station
-- boards rather than moving vehicles. Coordinates come from OpenStreetMap,
-- matched to RTT station codes by name.
CREATE TABLE IF NOT EXISTS rail_stations (
    code      TEXT PRIMARY KEY,
    name      TEXT,
    lat       REAL,
    lon       REAL,
    synced_at INTEGER
);

CREATE TABLE IF NOT EXISTS rail_services (
    uid           TEXT NOT NULL,
    station_code  TEXT NOT NULL,
    headcode      TEXT,
    operator_code TEXT,
    operator      TEXT,
    origin        TEXT,
    destination   TEXT,
    scheduled_ts  INTEGER,
    forecast_ts   INTEGER,
    delay_min     INTEGER,
    is_cancelled  INTEGER DEFAULT 0,
    platform      TEXT,
    coaches       INTEGER,
    leg           TEXT,
    seen_ts       INTEGER,
    PRIMARY KEY (uid, station_code)
);
CREATE INDEX IF NOT EXISTS rail_svc_station ON rail_services(station_code, scheduled_ts);
CREATE INDEX IF NOT EXISTS rail_svc_seen ON rail_services(seen_ts DESC);

-- Railway line geometry from OpenStreetMap, as individual segments. Estimated
-- train positions are snapped onto these so they sit on track rather than
-- cutting across country between stations.
CREATE TABLE IF NOT EXISTS rail_track (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lat1 REAL, lon1 REAL, lat2 REAL, lon2 REAL,
    min_lat REAL, min_lon REAL, max_lat REAL, max_lon REAL,
    fetched_at INTEGER
);
CREATE INDEX IF NOT EXISTS rail_track_bbox ON rail_track(min_lat, max_lat);

-- Estimated train positions. Derived from timings, never reported: see rail.py.
CREATE TABLE IF NOT EXISTS train_positions (
    uid          TEXT PRIMARY KEY,
    headcode     TEXT,
    operator     TEXT,
    origin       TEXT,
    destination  TEXT,
    lat          REAL,
    lon          REAL,
    bearing      REAL,
    from_code    TEXT,
    to_code      TEXT,
    from_name    TEXT,
    to_name      TEXT,
    progress     REAL,     -- 0..1 between the two calling points
    delay_min    INTEGER,
    state        TEXT,     -- at_station | between
    basis        TEXT,     -- actual | forecast: what the leg start relied on
    snapped_m    REAL,     -- distance moved when snapping to track
    leg_start_ts INTEGER,
    leg_end_ts   INTEGER,
    computed_ts  INTEGER,
    path_past    TEXT,     -- JSON [[lat,lon],...] already travelled
    path_future  TEXT,     -- JSON [[lat,lon],...] still to come
    calls        TEXT      -- JSON calling points with times
);
CREATE INDEX IF NOT EXISTS train_pos_ts ON train_positions(computed_ts DESC);

CREATE TABLE IF NOT EXISTS rail_samples (
    ts             INTEGER PRIMARY KEY,
    stations       INTEGER,
    services       INTEGER,
    cancelled      INTEGER,
    mean_delay_min REAL,
    on_time_pct    REAL
);
"""

# Columns added after the first release; applied on every start.
MIGRATIONS = [
    "ALTER TABLE fleet_samples ADD COLUMN in_use INTEGER",
    "ALTER TABLE train_positions ADD COLUMN path_past TEXT",
    "ALTER TABLE train_positions ADD COLUMN path_future TEXT",
    "ALTER TABLE train_positions ADD COLUMN calls TEXT",
    "ALTER TABLE transit_vehicles ADD COLUMN line_name TEXT",
    "ALTER TABLE transit_vehicles ADD COLUMN direction TEXT",
    "ALTER TABLE transit_vehicles ADD COLUMN origin_name TEXT",
    "ALTER TABLE transit_vehicles ADD COLUMN destination_name TEXT",
    "ALTER TABLE transit_vehicles ADD COLUMN journey_ref TEXT",
]

_local = threading.local()


def connect():
    """Per-thread connection. SQLite objects are not shareable across threads."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(config.DB_PATH), exist_ok=True)
        conn = sqlite3.connect(config.DB_PATH, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        _local.conn = conn
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    for stmt in MIGRATIONS:
        try:
            conn.execute(stmt)
        except sqlite3.OperationalError:
            pass  # already applied
    conn.commit()
    return conn


def rows(sql, args=()):
    return [dict(r) for r in connect().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = connect().execute(sql, args).fetchone()
    return dict(r) if r else None


def scalar(sql, args=(), default=None):
    r = connect().execute(sql, args).fetchone()
    if not r or r[0] is None:
        return default
    return r[0]
