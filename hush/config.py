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

SERVER_HOST = os.environ.get("HUSH_HOST", "127.0.0.1")
SERVER_PORT = int(os.environ.get("HUSH_PORT", "8000"))

# Bristol, for the initial map view.
DEFAULT_CENTER = (51.4545, -2.5879)
DEFAULT_ZOOM = 13
