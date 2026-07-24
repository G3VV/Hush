"""Configuration for the Hush collector and server."""

import os

CITY = os.environ.get("HUSH_CITY", "bristol")
GBFS_BASE = "https://gbfs.api.ridedott.com/public/v2"

FEEDS = {
    "free_bike_status": f"{GBFS_BASE}/{CITY}/free_bike_status.json",
    "vehicle_types": f"{GBFS_BASE}/{CITY}/vehicle_types.json",
    "system_pricing_plans": f"{GBFS_BASE}/{CITY}/system_pricing_plans.json",
    "geofencing_zones": f"{GBFS_BASE}/{CITY}/geofencing_zones.json",
    "station_information": f"{GBFS_BASE}/{CITY}/station_information.json",
    "system_information": f"{GBFS_BASE}/{CITY}/system_information.json",
}

DB_PATH = os.environ.get("HUSH_DB", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "hush.db"))

# Poll cadence. The feed advertises ttl=300, but vehicles report far more often
# than that, and a tighter interval sharpens trip start/end timestamps.
POLL_INTERVAL_S = int(os.environ.get("HUSH_POLL_INTERVAL", "60"))

# Static feeds (zones, stations, pricing) change rarely.
STATIC_REFRESH_S = int(os.environ.get("HUSH_STATIC_REFRESH", "21600"))  # 6h

HTTP_TIMEOUT_S = 45
USER_AGENT = "Hush/1.0 (Dott GBFS analytics; +https://github.com/g3vv/hush)"

# --- Trip inference thresholds -------------------------------------------------
# GPS scatter on a stationary scooter is typically <20m. Anything under this is
# treated as noise rather than a relocation.
DRIFT_M = 30.0
# A battery gain this large means a human swapped the battery or the vehicle
# went back to a warehouse -- not a customer trip.
SERVICE_FUEL_GAIN = 0.15
# Implied straight-line speed above this is not a scooter (max ~25 km/h);
# it is a van moving vehicles around.
MAX_RIDE_KMH = 45.0
# Absences longer than this that end somewhere new are more likely operational
# (overnight collection, workshop) than a single customer ride.
MAX_RIDE_S = 4 * 3600

# Retired vehicle ids are dropped after this long. Rental events, polls and
# fleet samples are never pruned -- they are the history.
RETAIN_DAYS = int(os.environ.get("HUSH_RETAIN_DAYS", "7"))

# --- Public transport ----------------------------------------------------------
# The DfT Bus Open Data Service publishes every operator's real-time vehicle
# positions nationwide as one GTFS-Realtime file, with no API key. It is ~2 MB a
# fetch, so the default interval is slower than the scooter poll.
BODS_GTFSRT_URL = os.environ.get(
    "HUSH_BODS_URL", "https://data.bus-data.dft.gov.uk/avl/download/gtfsrt")
TRANSIT_POLL_INTERVAL_S = int(os.environ.get("HUSH_TRANSIT_INTERVAL", "120"))
TRANSIT_ENABLED = os.environ.get("HUSH_TRANSIT", "1") != "0"

# Greater Bristol. (min_lat, min_lon, max_lat, max_lon)
BBOX = (
    float(os.environ.get("HUSH_BBOX_MIN_LAT", "51.38")),
    float(os.environ.get("HUSH_BBOX_MIN_LON", "-2.75")),
    float(os.environ.get("HUSH_BBOX_MAX_LAT", "51.55")),
    float(os.environ.get("HUSH_BBOX_MAX_LON", "-2.45")),
)

# Operators keep reporting vehicles long after they stop moving -- the tail of
# the feed reaches a day old. Anything older than this is not drawn as live.
TRANSIT_MAX_AGE_S = int(os.environ.get("HUSH_TRANSIT_MAX_AGE", "900"))
# Position trails are kept this long, then pruned.
TRANSIT_TRAIL_S = int(os.environ.get("HUSH_TRANSIT_TRAIL", "10800"))  # 3h

# --- Rail (Realtime Trains) ----------------------------------------------------
# A long-life refresh token from https://api-portal.rtt.io. Rail is simply
# skipped when this is unset -- nothing else changes.
#
# RTT's terms require the token stays server-side and never reaches a browser,
# which is why it is read from the environment and only ever used here.
RTT_TOKEN = os.environ.get("HUSH_RTT_TOKEN", "").strip()
RAIL_ENABLED = os.environ.get("HUSH_RAIL", "1") != "0"
# Each cycle costs one request per station (~19). The documented ceilings are
# 30/minute and 750/hour, so five minutes leaves plenty of headroom.
RAIL_POLL_INTERVAL_S = int(os.environ.get("HUSH_RAIL_INTERVAL", "300"))
RAIL_RETAIN_S = int(os.environ.get("HUSH_RAIL_RETAIN", str(3 * 86400)))

# Static infrastructure (stations, stops, ferry piers) from OpenStreetMap.
# Overpass rate-limits aggressively, so this is cached hard.
OVERPASS_URL = os.environ.get("HUSH_OVERPASS", "https://overpass-api.de/api/interpreter")
OSM_REFRESH_S = int(os.environ.get("HUSH_OSM_REFRESH", str(7 * 86400)))

SERVER_HOST = os.environ.get("HUSH_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("HUSH_PORT", "8000"))

# Bristol, for the initial map view.
DEFAULT_CENTER = (51.4545, -2.5879)
DEFAULT_ZOOM = 13
