# Hush

A live map of how **Bristol** moves. Shared scooters and bikes, every bus and coach on
the road, and the rail network — on one map, from open data.

| Mode | What you see |
|---|---|
| **Scooters & bikes** | ~3,100 rentable vehicles, battery, idle time, rental hotspots |
| **Buses & coaches** | ~175 live, route number, recorded track and the road ahead |
| **Trains** | Estimated positions on the railway, journey paths, live station boards |
| **Infrastructure** | Rail stations, bus stops, ferry piers, geofencing zones, parking bays |

Mode is drawn by shape — small dots are scooters, arrows are buses pointing their
heading, bars are trains, squares are stations — so colour stays free to mean battery
level. Everything on the map is clickable.

Alongside the map: an **Analytics** tab covering rentals, demand by hour, battery and
dead stock, bus operators and speeds, and rail punctuality; and a **Scooters** tab with
the fleet as sortable tables.

The core runs with **no API keys at all**. Two optional keys add more:
a free [BODS](https://data.bus-data.dft.gov.uk) key for bus route numbers and route
geometry, and a [Realtime Trains](https://api-portal.rtt.io) token for rail.

No dependencies beyond the Python standard library. Leaflet loads from a CDN; run
`sh scripts/vendor-leaflet.sh` to keep a local copy instead, which the page picks up
automatically.

```bash
python3 -m hush.server          # collector + dashboard on http://127.0.0.1:8000
```

Then open <http://127.0.0.1:8000>. Data lands in `data/hush.db` (SQLite).

## Shared scooters and bikes

Bristol's scheme is run by Dott, whose GBFS feed is public and needs no key. One thing
about it shapes the whole design, so it is worth stating plainly.

**Dott rotates each vehicle's `bike_id` after every rental.** The GBFS specification asks
operators to do this precisely so that riders cannot be followed from trip to trip. Measured
against the live Bristol feed: over 20 minutes, 134 vehicles left the feed and 129 fresh
UUIDs appeared — and **not one** of the departed IDs ever came back.

The consequence is that **per-vehicle journey history does not exist and cannot be
reconstructed**, by this or any other tool reading the public feed. A vehicle that leaves the
feed is gone for good under that identity. There is no way to know which rental start goes
with which rental end, so no origin-destination pairs, no per-vehicle trip lists, and no
route history. Any tool claiming to show you those from this feed is guessing.

What *is* directly observable, and what Hush is built on:

| Observable | How |
|---|---|
| Every rentable vehicle's position, battery, range | Straight from the feed |
| A rental **starting** | A vehicle disappears from the feed |
| A rental **ending** | A brand-new ID appears |
| How long a vehicle sat before being taken | Time between its arrival and disappearance |
| Battery drain while parked | Change in charge across polls |
| Operational moves (vans, battery swaps) | Position changes that *keep* the same ID, so no rental happened |

Two headline numbers are **estimates**, derived rather than measured, and the UI labels them
as such:

- **Rides in progress** — missing IDs pile up forever (they retire on rental), so they cannot
  be counted directly. Instead the largest available-count ever seen is taken as the deployed
  fleet size, and rides in progress is the shortfall against it.
- **Mean ride length** — from Little's Law: with `L` vehicles out at any moment and rentals
  starting at rate `λ`, the average ride lasts `L / λ`. This needs only counts, never a link
  between a particular start and end, which is exactly what the ID rotation denies us.

Both depend on having seen a quiet period to calibrate the fleet size, so they stay blanked
out until the collector has ~12 hours of data including one overnight lull. The dashboard
shows how much longer it needs rather than printing a number it cannot stand behind.

## Buses, trains and ferries

Three modes, three very different levels of availability. Measured, not assumed:

| Mode | What is available | Source |
|---|---|---|
| **Bus / coach** | Live positions, ~175 in Bristol at any moment, updated every couple of minutes | DfT Bus Open Data Service, national GTFS-Realtime, **no key** |
| **Rail** | Live departure boards, delays, platforms, cancellations — but **no train positions** | Realtime Trains (token required) + OpenStreetMap for station coordinates |
| **Ferry** | Pier positions only | OpenStreetMap |

### How trains are placed on the map

Realtime Trains returns rich live *timing* data: which services call where, forecast
against schedule, delays, platforms, cancellations, operators and formation lengths. What
it does not return, anywhere, is a coordinate. Neither the service responses nor the
reference endpoints (`/data/stops`, `/data/locations_ungrouped`) carry a latitude or
longitude — those return only codes and names.

So train positions here are **reconstructed, not reported**, and the UI says so on every
train. The method:

1. Take the service's ordered calling points and their times, preferring
   `realtimeActual` — the time a train genuinely passed a point — over a forecast.
2. Find which leg *now* falls in: dwelling at a station, or running between two.
3. Route between those two stations along real railway geometry — a graph of ~34,000
   OpenStreetMap track segments, shortest path by Dijkstra — and place the train at the
   right fraction *along that path*, not along a straight line.

Step 3 is what makes it accurate. A straight line between distant stations can sit
kilometres from the railway: the midpoint of Cheltenham Spa → Bristol Parkway is 2.6 km
off the actual line. Routed, trains sit on the track — measured at 0 m for
Salisbury → Warminster, Cheltenham → Parkway (62 km via Gloucester) and
Weston Milton → Worle. The routing also sanity-checks against reality: Temple Meads →
Lawrence Hill computes as 1.7 km, Temple Meads → Parkway as 9.4 km.

Outside the track bounding box there is no geometry to route along, so those trains fall
back to straight-line interpolation, are drawn with a dashed marker, and say in the panel
that no mapped track was near enough to be confident.

Clicking a train draws its whole journey: solid for the legs already travelled, dashed
for those still to come, with every calling point marked. Both follow real track.

**The limits.** This is an estimate and only as good as the timings behind it. A train
between two widely spaced calling points, or running to a stale forecast, will drift.
Nothing here is GPS.

Stations are matched from OpenStreetMap to RTT codes by name — 19 in the map area (used
for boards) and ~330 across the wider region (used to give calling points coordinates).
The Bristol-area stations that do not match are heritage lines — Avon Valley, the SS
Great Britain railway — genuinely not on the national network.

Rail is **off unless you supply a token**; nothing else changes when it is absent.

```bash
HUSH_RTT_TOKEN=your_token_here python3 -m hush.server
```

Get one from <https://api-portal.rtt.io>. The token you are issued is a long-life
*refresh* token: Hush exchanges it for a 20-minute access token as needed. Note RTT's
terms — **the token must never reach an end-user application**, so it is read from the
environment, used only server-side, and never sent to the browser. Rate limits (30/min,
750/hour) are respected: one cycle costs one request per station, every five minutes by
default.

Two things the bus feed does not hand over cleanly, both handled rather than papered over:

- **Stale vehicles.** Operators leave dead vehicles in the feed for hours — the tail
  reaches a full day old. Anything whose last report is older than 15 minutes
  (`HUSH_TRANSIT_MAX_AGE`) is dropped instead of being drawn as a bus that is not there.
  In practice this removes about half the vehicles inside the bounding box.
- **Route names.** The open feed carries only the operator's internal `route_id`
  ("7444"). With a free BODS key (`HUSH_BODS_KEY`) the SIRI-VM feed supplies the number
  on the front of the bus — m1, A1, 75 — plus origin and destination, matched to the
  GTFS-RT vehicles by their shared vehicle reference.

### Where a bus has been, and where it is going

The **past** is measured: the positions actually recorded for that vehicle. The
**future** is the operator's own route alignment, taken from the TransXChange timetables
on BODS, which carry the real road geometry, and cut at the point on that line nearest
the bus. It is where the bus is *routed* to go, not a prediction of when it arrives, and
it is drawn dashed to say so.

Line numbers repeat across the country — a "24" exists in a dozen places — so shapes are
kept only where they overlap the Bristol area. Without that filter a Stagecoach 24 from
another region matched, putting the route 59 km out to sea.

Unlike the scooters, **bus fleet numbers are stable**, so buses genuinely can be followed:
each one has a recorded track, a real distance travelled, and a speed derived between
polls (the feed has no speed field). Clicking a bus draws its trail.

GTFS-Realtime is protobuf, and Hush has no third-party dependencies, so `hush/gtfsrt.py`
decodes the wire format directly — about 150 lines, covering only the vehicle-position
subset that is actually used.

## How history is built

The feed is a snapshot with no history endpoint, so history is accumulated by polling. Each
poll is diffed against the last:

- a vehicle **present** and stationary → its current stay is extended
- a vehicle that **vanishes** → a rental started (or ops collected it): a `pickup` event
- a **new ID** appears → a rental ended (or a fresh deployment): a `dropoff` event
- a vehicle that **moved while keeping its ID** → an operational move, classified as a van
  relocation (faster than the vehicle can travel), a battery swap (charge went *up*), or GPS
  drift (under 30 m — measured jitter on parked vehicles peaks around 9 m)

Retired IDs are pruned after 7 days (`HUSH_RETAIN_DAYS`). Rental events, poll logs and fleet
samples are never pruned — they are the historical record.

## Configuration

Settings come from environment variables, or from a `.env` file in the project root
(next to this README). Copy the template and fill in what you need:

```bash
cp .env.example .env
```

```ini
# .env
HUSH_RTT_TOKEN='eyJhbGci…'
HUSH_PORT=9067
```

`KEY=VALUE` per line, `#` for comments, quotes optional but wise for tokens. A real
environment variable always beats the file, so `HUSH_PORT=9000 python3 -m hush.server`
still wins for that run. `.env` is gitignored; `.env.example` is the committed template.
Point somewhere else with `HUSH_ENV_FILE=/path/to/file`.

All optional, via environment variables or `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `HUSH_CITY` | `bristol` | Scooter scheme city slug (`nottingham`, `milton-keynes`, …) |
| `HUSH_POLL_INTERVAL` | `60` | Seconds between polls. The feed itself refreshes every ~2 min |
| `HUSH_DB` | `data/hush.db` | SQLite path |
| `HUSH_RETAIN_DAYS` | `7` | How long to keep retired vehicle IDs |
| `HUSH_HOST` / `HUSH_PORT` | `127.0.0.1:8000` | Bind address |
| `HUSH_TRANSIT` | `1` | Set `0` to skip buses entirely |
| `HUSH_TRANSIT_INTERVAL` | `120` | Seconds between bus polls. The national feed is ~2 MB a fetch |
| `HUSH_TRANSIT_MAX_AGE` | `900` | Drop bus positions older than this |
| `HUSH_BODS_KEY` | unset | BODS API key: adds route numbers and route geometry |
| `HUSH_TRANSIT_TRAIL` | `10800` | How long bus tracks are kept |
| `HUSH_BBOX_*` | Greater Bristol | `MIN_LAT`, `MIN_LON`, `MAX_LAT`, `MAX_LON` |
| `HUSH_RTT_TOKEN` | unset | Realtime Trains refresh token. Rail is skipped without it |
| `HUSH_RAIL` | `1` | Set `0` to skip rail even with a token |
| `HUSH_RAIL_INTERVAL` | `300` | Seconds between rail polls (one request per station) |
| `HUSH_RAIL_RETAIN` | `259200` | How long rail service records are kept |
| `HUSH_TRAIN_POSITIONS` | `1` | Set `0` to skip estimated train positions |
| `HUSH_TRAIN_MAX` | `25` | Services positioned per cycle (one API request each) |
| `HUSH_TRACK_SNAP_MAX` | `600` | Metres: reject a track snap further than this |

Run the collector headless (no dashboard), or the dashboard against an existing database:

```bash
python3 -m hush.collector             # collect only
python3 -m hush.server --no-collector # serve only
```

## API

| Endpoint | Returns |
|---|---|
| `/api/health` | Collector status, poll count, rentals tracked |
| `/api/live` | Current vehicles; filter by `type`, `min_fuel`, `max_fuel`, `status` |
| `/api/vehicle/<id>` | One vehicle: state, stays, operational moves |
| `/api/stats?hours=24` | Everything on the analytics tab |
| `/api/hotspots?hours=24` | Rental and drop-off hotspots, grid-clustered |
| `/api/balance?hours=24` | Net gain/drain per area — where rebalancing is needed |
| `/api/events?hours=24` | Raw rental start/end events |
| `/api/transit` | Live buses and coaches |
| `/api/transit/vehicle/<id>` | One bus: state plus its recorded track |
| `/api/infrastructure?kind=rail_station` | Stations, stops and piers |
| `/api/rail` | Rail stations with live service counts and average delay |
| `/api/rail/station/<code>` | One station's live departure board |
| `/api/trains` | Estimated train positions with leg and progress |
| `/api/leaderboard?hours=24` | Longest idle, flattest battery, fastest rented |
| `/api/zones`, `/api/stations`, `/api/pricing` | Cached static feeds |

## Layout

```
hush/          collectors, analytics and server (stdlib only)
  config.py    tunables and feed URLs
  db.py        SQLite schema
  collector.py scooters and bikes: polling and event inference
  gtfsrt.py    minimal GTFS-Realtime protobuf decoder
  transit.py   buses from BODS, infrastructure from OpenStreetMap
  rail.py      Realtime Trains: token refresh and live station boards
  analytics.py aggregations
  server.py    HTTP API + static files
web/           dashboard (vanilla JS, Leaflet)
scripts/       optional Leaflet vendoring
data/hush.db   created on first run
```

Pricing used for revenue estimates is Bristol's at time of writing: £1 unlock + £0.25/min,
read from the live `system_pricing_plans` feed.

Scooter and bike data © Dott, via their public GBFS feed. Bus and coach positions © the
operators, via the DfT Bus Open Data Service (Open Government Licence). Rail timings via
Realtime Trains, subject to <https://www.realtimetrains.co.uk/legal/>. Station, stop and
pier positions © OpenStreetMap contributors (ODbL). Basemap © OpenStreetMap contributors,
© CARTO.
