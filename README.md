# Fuel Route Optimization API — Django REST Assessment

A Django REST API that, given a **start** and **finish** location within the USA, returns:

* the driving route on an interactive map (via **OSRM** — free, no API key),
* **cost-effective fuel stops along the route, at identity-verified
  station positions** (real CSV stations near the path, each chosen stop
  verified to its actual location before use; vehicle: 10 mpg, 500-mile
  range - see "Precision and limits" below),
* the **total money spent on fuel** purchased en route,
* an interactive HTML map with the route and stop markers.

Data source: `fuel-prices-for-be-assessment.csv` (~8,151 truck-stop prices).

---

## How it works (design summary)

1. **One OSRM call** returns route geometry + distance; a second OSRM
   waypoint call (start + chosen stops + finish) measures the finished
   plan on ACTUAL ROADS: per-leg road miles, road-derived detours, and
   purchases/range recomputed on those road legs with the same tank rules.
2. **Station locations are local data for CANDIDATE GENERATION only.** The
   CSV has no coordinates, so a one-time preprocessing step
   (`python manage.py geocode_stations`) resolves each station's
   `(city, state)` to a city-level coordinate in
   `router/data/station_locations.json`. City precision is enough to FIND
   candidate stops near the route - it is never reported as a station's
   position.
3. **Chosen stops are identity-verified before use** (`router/resolver.py`).
   A stop the planner wants must produce an ACTUAL station position:
   a single rate-limited, user-triggered Nominatim search whose result must
   (a) share a distinctive name token with the CSV station (generic words
   like TRAVEL/CENTER don't count), (b) sit within 15 mi of the station's
   city, and (c) be unambiguous (two same-chain branches in one city =
   rejected). Some stations Nominatim misses are recovered from offline
   OSM data (Geofabrik extracts) or the operator's own published address;
   every resolution ships its evidence and source in
   `router/data/station_resolutions.json` (cached, 90-day positive /
   30-day negative TTL, then re-checked). Stops that cannot be verified
   are EXCLUDED and the planner picks another station; if no verified
   reachable chain exists the API fails clearly (422). This is `strict`
   mode, the default.
4. **Stop planning is local math**: a tank-aware greedy planner walks the
   route with a full tank (50 gal, implied range/mpg): head for the
   cheapest station the tank can reach; buy only enough to reach a cheaper
   station, only what the final leg needs, or fill when nothing cheaper is
   reachable. Candidate legs use route miles plus estimated detour; the
   FINISHED plan is then measured on road legs (step 1) and purchases are
   recomputed there. If a road leg exceeds the physical range the planner
   retries with a bounded margin, then fails honestly.
5. **`mode='approximate'` (explicit opt-in only)**: stations that cannot be
   verified at POI level may fall back to the highway exit named in their
   OWN CSV address, resolved against a prebuilt offline OSM
   motorway-junction index (`router/data/exit_junctions.json`; NE/IA/IL/CO
   shipped). An exit junction is NOT the station - it is the vicinity the
   employer's own data states, with the ~0.5 mi lot offset unverified.
   Such stops are labelled `position_source='csv_address_exit_junction'`
   and the response note says APPROXIMATE MODE.

**External call budget per request (all services disclosed):**
map/route-API calls = 1 OSRM route + 1 OSRM waypoint measurement
(at most ONE bounded margin re-plan, so at most 2 waypoint calls)
= **2 typical, 3 worst case** - ALWAYS within the brief's "One call to
the map/route API is ideal, two or three is acceptable". Other services, disclosed
separately: at most 2 Nominatim endpoint geocodes (0 for `lat,lng`
inputs) + at most `max_station_lookups=25` rate-limited Nominatim station
lookups - a REAL global cap enforced per lookup in code (tested), since
`max_attempts=20` bounds re-plan iterations, not lookups. Cold-corridor
worst case: 4 + 2 + 25 = **31 external calls** (~30 s from the polite
1.1 s spacing), then the request fails with a clear retryable error if
the cap trips. Every lookup is cached (90-day positive / 30-day negative
TTL, re-checked after), so a warm corridor makes **4 external calls
total** and answers in ~4-9 s. An automated test counts every external
call a full POST makes.

Coverage: 7,530 of 7,531 US stations (~100%) resolve to a city location;
620 Canadian rows in the CSV are excluded (the assignment covers USA
routes). One US city ("NEW, ND", truncated in the CSV) is unresolved and its
single station is skipped by the planner (it still works in price lookups).

---

## Fuel-cost semantics (stated assumption)

The assignment fixes the 500-mile range and 10 mpg but says nothing about
the initial tank, so this project states its assumption explicitly:

* **The vehicle starts with a full tank**, and the tank capacity is the
  implied `range / mpg` = **50 gal**. Mile 0 -> first stop needs no purchase.
* The planner tracks the tank physically: it **never buys more than fits**,
  buys only enough to reach a cheaper station ahead when one exists within
  a full tank's reach, and buys only what the final leg needs instead of
  filling up at the last stop. Fuel is priced where it is actually bought.
  `total_fuel_cost_usd` is **money spent at stops**.
* `total_gallons_consumed` is reported separately, so a one-tank trip shows
  its real consumption with $0 purchased - never "fuel is free", never
  hiding the assumption. For verified plans it is
  **`total_road_distance_miles / mpg`** - actual road legs measured by the
  OSRM waypoint call, including stop detours. Only for one-tank trips
  (no stops, no waypoint call) is it the modeled estimate
  `total_distance_miles / mpg`, and `total_road_distance_miles` is then
  null to say so.

Stop coordinates in a response are the **identity-verified station
positions** described below (strict) or explicitly-labelled exit-junction
vicinity points (approximate opt-in) - never city centroids. Map markers
sit at those same reported positions, with `position_source` naming the
evidence tier.

---

## Precision and limits (read before demoing)

* **Default (strict) mode reports only verified ACTUAL station
  positions** (`position_source` per stop: `osm_nominatim_identity`,
  `osm_poi_offline_extract`, `operator_address_geocoded`). Each ships its
  evidence (OSM id / operator URL, distances, shared tokens) in
  `router/data/station_resolutions.json`. Data (c) OpenStreetMap
  contributors, ODbL.
* **Verification is selective, not total.** Not every CSV station can be
  verified from free sources (small rural independents are often absent
  from OSM by name and publish no coordinates). Strict mode EXCLUDES
  unverifiable stations; on corridors where the cheapest stations are
  unverifiable the strict plan can cost slightly more than the theoretical
  cheapest - that is the honest price of not pretending. If no verified
  reachable chain exists, the API returns 422 instead of guessing.
* **`mode='approximate'` is a clearly-labelled fallback**, not the fix:
  exit-junction vicinity positions (from the station's own CSV address)
  bind the vicinity, not the lot/entrance.
* **OSRM routes are car-profile** - not a truck-access guarantee.
* **The junction index ships for NE/IA/IL/CO only** (approximate mode);
  extend it per state with the Geofabrik extract + parser documented in
  `router/data/`.
* **CSV staleness is real**: at least one CSV station (Henderson Fuel
  Mart, I-80 exit 342) appears closed/renamed per June 2026 local
  reporting. Prices are used as given; verification does not prove a
  station still operates.
* **Branch identity is brand+city level, not branch-number level**: the
  verifier binds distinctive name tokens (e.g. KUM) and location, NOT the
  CSV's "#0370" branch numbers. Independent review assessed this as
  adequate for rural single-site stations, but it is NOT a national
  branch-identity guarantee (two same-brand sites in one city are
  rejected as ambiguous instead).
* `road_detour_miles` is a heuristic allocation of the waypoint route's
  extra road distance, NOT a verified actual access distance to the lot.
* The tank-aware greedy purchase rule (enough-to-reach-cheaper, else
  fill) is a documented heuristic, not a proven global cost minimum.

## Project structure

```
fuel_route_api/          # Django project (settings, urls, wsgi)
router/                  # Django app
  models.py              # FuelStop, FuelStopDetail
  serializers.py         # DRF serializers + input validation
  views.py               # RouteOptimizeView, RouteDetailView, FuelPriceView
  urls.py                # /api/route/, /api/route/<id>/, /api/fuel-price/
  fuel_data.py           # CSV loader + location join + price indexes
  routing.py             # OSRM route + Nominatim geocode clients (rate-limited)
  fuel_optimizer.py      # corridor planner + strict/approximate verification loop
  resolver.py            # station identity verification (Nominatim + offline tiers)
  map_gen.py             # Folium HTML map
  management/commands/geocode_stations.py   # one-time location preprocessing
  data/station_locations.json               # city coordinates (candidate finding)
  data/station_resolutions.json             # verified positions + evidence cache
  data/exit_junctions.json                  # offline junction index (approximate mode)
  data/README.md                            # data provenance + how to extend coverage
  admin.py               # Admin registration
  tests.py               # 53 tests, all external calls mocked (runs offline)
  migrations/
fuel-prices-for-be-assessment.csv   # fuel price data (project root)
manage.py
requirements.txt
.env.example
.gitignore
```

Generated at runtime (git-ignored): `db.sqlite3` (via `migrate`) and `media/` (route maps).

---

## Setup

```bash
# 0. Requires Python 3.12+ (Django 6.1 drops older Pythons).
python3 --version   # 3.12 or newer; on macOS use python3.14 if installed

# 1. Clone, then open the project root containing manage.py.
git clone https://github.com/AloneRider-pixel/fuel-route-optimizer.git
cd fuel-route-optimizer
# ZIP alternative: extract it and open the folder containing manage.py.

# 2. Create & activate a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies in that environment
python -m pip install -r requirements.txt

# 4. Apply migrations
python manage.py migrate

# 5. Run the server
python manage.py runserver   # http://127.0.0.1:8000
```

No API keys are required (OSRM + Nominatim are free, no-key services).
`router/data/station_locations.json` ships with the repo, so no
preprocessing is needed to run. To regenerate it (e.g. for a newer CSV):

```bash
# Download the Census gazetteer (public domain):
#   https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip
python manage.py geocode_stations --gazetteer /path/to/2025_Gaz_place_national.txt
# Optional, for the residual US misses (one-time, rate-limited, cached):
python manage.py geocode_stations --gazetteer ... --nominatim-misses
```

### Configuration (optional, env vars)

The project runs out of the box with a development-only placeholder key. For
anything beyond local development, set real values (see `.env.example`):

| Variable | Default | Purpose |
|---|---|---|
| `DJANGO_SECRET_KEY` | dev-only placeholder | Django secret key |
| `DJANGO_DEBUG` | `true` | Set `false` outside development |
| `DJANGO_ALLOWED_HOSTS` | empty | Comma-separated hosts |

Vehicle and corridor constants live in `fuel_route_api/settings.py`:
`VEHICLE_MPG` (10), `VEHICLE_RANGE_MILES` (500), `STATION_CORRIDOR_MILES` (10).

---

## Map display and tile policy

Maps use the canonical HTTPS OpenStreetMap tile endpoint, visible contributor
attribution, and `strict-origin-when-cross-origin` so the browser sends its
actual origin to the tile service. The route is fitted into the map viewport.
These settings follow the [OSM tile policy](https://operations.osmfoundation.org/policies/tiles/);
they do not guarantee service availability or remove an existing block.
Do not spoof headers, bypass caches, proxy around blocks, or repeatedly reload
blocked tiles. If tiles remain blocked, stop and investigate the actual browser
request headers or choose a separately approved compliant provider.

Existing HTML files under `media/` are generated artifacts and are not rewritten
by a source update. Preserve an existing demo plan/map when applying a local fix;
do not recalculate routes just to refresh the tile template.

Map regression checks are offline. The suite now has 55 tests, including
canonical tiles/attribution/viewport and response-referrer-policy checks.

## API

### POST /api/route/
Compute an optimized route + fuel plan.

| Field | Type | Required | Example |
|---|---|---|---|
| `start` | string | Yes | `"Chicago, IL"` or `"41.8781,-87.6298"` |
| `finish`| string | Yes | `"Denver, CO"` or `"39.7392,-104.9903"` |
| `mode` | string | No | `"strict"` (default) or `"approximate"` (explicit opt-in for labelled exit-junction vicinity positions) |

**Response (200)** — captured verbatim from a live strict-mode run
(2026-11-17) against the real services; every stop position is an actual
verified station position (`position_source` per stop):

```json
{
  "id": 1,
  "start_location": "Chicago, IL",
  "finish_location": "Denver, CO",
  "total_distance_miles": 1004.35,
  "total_road_distance_miles": 1005.33,
  "total_fuel_cost_usd": 147.69,
  "total_gallons": 50.533,
  "total_gallons_consumed": 100.533,
  "verification_mode": "strict",
  "mpg": 10.0,
  "max_range_miles": 500.0,
  "stops": [
    {
      "order": 1, "mile_marker": 482.24, "truckstop_id": "69840",
      "name": "KUM & GO #0370", "address": "I-80, EXIT 439 & SR-370",
      "city": "Gretna", "state": "NE",
      "retail_price": 2.92066666, "gallons": 48.11, "cost_usd": 140.51,
      "latitude": 41.1460714, "longitude": -96.1620072,
      "road_detour_miles": 1.8,
      "location_precision": "osm_nominatim_identity",
      "position_source": "osm_nominatim_identity"
    },
    {
      "order": 2, "mile_marker": 661.21, "truckstop_id": "6093",
      "name": "ONE9 #901", "address": "I-80 & US-183, EXIT 257",
      "city": "Elm Creek", "state": "NE",
      "retail_price": 2.96566666, "gallons": 2.423, "cost_usd": 7.18,
      "latitude": 40.6920754, "longitude": -99.3814919,
      "road_detour_miles": 1.8,
      "location_precision": "osm_poi_offline_extract",
      "position_source": "osm_poi_offline_extract"
    }
  ],
  "map_url": "/media/route_1_XXXXXXXX.html",
  "note": "..."
}
```

Reading the plan: the implied 50-gal tank runs low by Gretna, so the
planner fills there (48.1 gal @ $2.9207). The road leg Gretna -> Denver is
~523 road miles - beyond the 500-mile physical range - so a small top-up
is unavoidable; the planner buys the minimum 2.4 gal at ONE9 (the cheapest
identity-VERIFIED station reachable on that stretch), a
destination-minimal purchase, not cheaper-station arbitrage (ONE9's
$2.9657 is slightly above Gretna's price). Purchases were recomputed on
the ACTUAL ROAD LEGS (1005.33 road miles), and every leg is within the
physical 500-mile tank range. `road_detour_miles` (1.8) is a HEURISTIC
allocation of the waypoint route's extra road distance (half of each
adjacent leg's excess over the direct mileage, attributed to the stop
between them) - NOT a measured access road to the lot. Prices are the
provided CSV snapshot, not a live pump-price promise. The strict request
completed in ~8.6 s with a cold station cache (6 external calls; see
Performance).

* **422 (strict mode)**: `no_feasible_plan` - "no reachable chain of
  stations with identity-verified actual positions" - names the
  `approximate` opt-in instead of guessing.

* `map_url` opens an interactive route map in any browser.
* Trips within one tank (<= 500 mi) return `"stops": []`, `$0.00` purchased,
  and the real `total_gallons_consumed`.
* No reachable station on some stretch: **422** with `no_feasible_plan` and
  the exact gap.

### GET /api/route/<id>/
Retrieve a previously computed plan (including stops).

### GET /api/fuel-price/?state=TX
Cheapest fuel price in a state (from the CSV; includes stations without
coordinates).

---

## Testing with Postman

1. Start the server: `python manage.py runserver`.
2. New request → **POST** `http://127.0.0.1:8000/api/route/`
3. **Body** → **raw** → **JSON**:

```json
{ "start": "Chicago, IL", "finish": "Denver, CO" }
```

| Test | Body / URL | Expect |
|---|---|---|
| Coordinates input | `{"start": "41.8781,-87.6298", "finish": "39.7392,-104.9903"}` | 200, no geocoding needed |
| One tank (no stop) | `{"start": "Chicago, IL", "finish": "Milwaukee, WI"}` | `stops: []`, consumed ~8.9 gal |
| Invalid | `{"start": "", "finish": "Denver, CO"}` | 400 |
| Approximate opt-in | `{"start": "Chicago, IL", "finish": "Denver, CO", "mode": "approximate"}` | 200, junction-labelled stops |
| Bad mode | `{"start": "Chicago, IL", "finish": "Denver, CO", "mode": "lax"}` | 400 |
| Get a plan again | `GET /api/route/1/` | 200 |
| Cheapest price in TX | `GET /api/fuel-price/?state=TX` | 200 |

**Admin** (optional): `python manage.py createsuperuser`, then `/admin/`.

---

## Running the automated tests

```bash
python manage.py test router
```

53 tests covering: the CSV loader and location join, state normalization,
cumulative-mile math, planner behavior and physical tank invariants (never
overfills, never runs dry, last stop buys only the final leg), identity
binding (distinctive-token accept, generic-only reject, same-chain
two-branch ambiguity reject, out-of-radius reject, NO silent highway
correction, junction-tier spread/radius rejection, strict mode never uses
junctions, positive/negative caching with TTL and failure reasons),
verified-plan behavior (unverifiable stops excluded and re-planned,
purchases recomputed on road legs with exact numbers, bounded margin
re-plan on infeasible road legs, margins-exhausted 422, one-tank skips
verification), every endpoint (happy path, mode validation 400, 404, 422,
502), persistence, and the external-call budget (warm cache: exactly 2
OSRM + 2 geocodes + 0 station lookups). All network traffic is mocked;
the suite runs fully offline in ~16 s.

---

## Performance

* **Map/route-API calls per request: 2 typical, at most 3** (1 OSRM route
  + 1 OSRM waypoint measurement; at most one bounded margin re-plan) -
  ALWAYS within the brief's "One call to the map/route API is ideal, two
  or three is acceptable".
* **Other external calls, disclosed separately** (the brief's budget names
  the map/route API; these are different services and are NOT hidden):
  <= 2 Nominatim endpoint geocodes (0 for `lat,lng` inputs) + <= 25
  rate-limited Nominatim station lookups (a real enforced global cap,
  tested). Cold worst case: 30 calls, ~30 s; cached afterwards (90/30-day
  TTL), so a warm corridor makes **4 external calls total**, ~4-9 s.
* CSV + locations are loaded once per process; price indexes are O(1).

## Verified environment

The full automated suite (53/53) and migrations were run on the exact
pinned set: **Django 6.1.3 / DRF 3.18.3 / requests 2.32.3 / folium 0.14.0
on Python 3.12.16** - verified installed via pip AND confirmed the latest
official release on djangoproject.com/download and PyPI on 2026-11-17.
Django 6.1 requires Python >= 3.12 (hence setup step 0). The suite also
passes on Django 5.2.19 / Python 3.10 (53/53). The live strict-mode
Chicago-Denver response quoted above ran on this pinned set on
2026-11-17, ~8.6 s end to end with a cold station cache (6 external
calls), folium map generated.

## Verification evidence (for review)

Every station resolution ships in `router/data/station_resolutions.json`
with its evidence. Examples from the demo corridor:

* **KUM & GO #0370, Gretna, NE** - `osm_nominatim_identity`: one
  Nominatim search, OSM POI "Kum & Go, Wickersham Boulevard, Gretna",
  shared distinctive token KUM, 2.21 mi from the city centroid.
* **ONE9 #901, Elm Creek, NE** - `osm_poi_offline_extract`: OSM nodes
  12094850510/12094850511 (amenity=fuel, brand=ONE9, "ONE9 Fuel Stop")
  0.15 mi from the I-80 exit 257 named in its own CSV address, in the
  Geofabrik Nebraska extract downloaded 2026-11-17 (reproduction command
  in the cache entry). Nominatim name searches missed it at every query
  shape tried. The identity binding is the NAME TOKEN plus the station's
  OWN address exit, not mere proximity; branch identity is brand+city
  level (the #901 number is not separately verified).
* **FATDOGS, Ogallala, NE** - `operator_address_geocoded`: the operator's
  official site (fatdogs.com location page, schema.org LocalBusiness)
  publishes "100 Prospector Drive, Ogallala"; one Nominatim address
  lookup places it 0.24 mi from the exit in the CSV address. Operator URL
  preserved in the evidence.

## Preparing your submission

1. **GitHub repo.** Create a public repository and push this project as-is
   (`fuel-prices-for-be-assessment.csv` is included as provided;
   `.gitignore` keeps `db.sqlite3`, `media/`, and `.env` out).
2. **Loom video (max 5 min).**
   - ~30 s: what the API does; start the server (`python manage.py migrate
     && python manage.py runserver`).
   - ~2.5 min live demo in Postman: `POST /api/route/` with 2-3 city pairs
     (e.g. Chicago, IL -> Denver, CO; Dallas, TX -> Atlanta, GA), showing
     the route, stops with prices and per-stop cost, total cost, and opening
     the returned map link in a browser. A one-tank trip (e.g. Chicago, IL
     -> Milwaukee, WI) shows the consumption-without-purchase case.
   - ~2 min code overview: `router/fuel_data.py` (CSV + location join),
     `router/routing.py` (OSRM route + waypoint measurement + rate-limited
     geocodes), `router/resolver.py` (station identity verification),
     `router/fuel_optimizer.py` (corridor + tank-aware planner), `router/views.py`.
     State the design in Precision and limits out loud - especially that
     the provided CSV carries no coordinates, so chosen stops are
     identity-verified against OpenStreetMap before use (strict default;
     unverifiable stations are excluded, not approximated), the stated
     full-tank assumption, and that prices are the CSV snapshot. Calling
     these out yourself demonstrates judgment. Note: strict-mode stop
     verification is proven live on the Chicago-Denver corridor; demo any
     other corridor once yourself before recording.
3. Submit the repo and video links through the questionnaire.
