# Station location data

- `station_locations.json` - city-level coordinates per CSV (city, state),
  used ONLY to find candidate stops near the route. Never reported as a
  station position. Sources: US Census Gazetteer 2025 place file (public
  domain) + a one-time rate-limited cached Nominatim pass for misses.
- `station_resolutions.json` - the identity-verification cache: every
  verified ACTUAL station position with its evidence (OSM ids / operator
  URL / distances / shared name tokens), 90-day positive / 30-day negative
  TTL. Shipped entries document how each was established.
- `exit_junctions.json` - offline motorway-junction index
  (state|highway|exit -> OSM junction nodes) for the `approximate` mode
  fallback. Covers NE/IA/IL/CO, built 2026-11-17 from Geofabrik state
  extracts ((c) OpenStreetMap contributors, ODbL).

## Extending junction coverage to another state (approximate mode)

    curl -LO https://download.geofabrik.de/north-america/us/<state>-latest.osm.pbf
    pip install osmium
    python router/data/build_exit_junction_index.py <state-2-letter> <state>-latest.osm.pbf
    # merge the printed JSON into exit_junctions.json
