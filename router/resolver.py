"""
Verified positions for CHOSEN fuel stops - identity-bound, cached, honest.

A stop the planner wants to use is verified against OpenStreetMap via a
SINGLE user-triggered Nominatim search (policy: end-user-triggered app use
is permitted; max 1 req/s enforced by the caller; identifying User-Agent;
results cached - see https://operations.osmfoundation.org/policies/nominatim/).
Data (c) OpenStreetMap contributors, ODbL.

Identity binding (a nearby result is NOT enough):
  * the POI must share at least one DISTINCTIVE name token with the CSV
    station name (generic tokens like TRAVEL/CENTER/FUEL do not count), and
  * it must lie within VERIFY_RADIUS_MI of the station's city position, and
  * it must be unambiguous: after clustering candidates that sit at
    effectively the same spot, exactly one matching cluster may remain
    (two different branches of the same chain in one city -> REJECTED).

Every resolution - positive or negative - is cached with its evidence and a
timestamp; entries are re-checked after POSITIVE_TTL_DAYS / NEGATIVE_TTL_DAYS
instead of being treated as immutable truth. Unresolvable stations are not
used by the planner (it selects another station), never silently approximated.
"""

import json
import os
import re
import time

from django.conf import settings

VERIFY_RADIUS_MI = 15.0
SAME_PLACE_MI = 0.5          # candidates closer than this are one place
JUNCTION_RADIUS_MI = 25.0    # exit junction may be this far from the named town
JUNCTION_SPREAD_MI = 3.0     # junction nodes further apart than this = ambiguous
POSITIVE_TTL_DAYS = 90
NEGATIVE_TTL_DAYS = 30

# Tokens that say nothing about WHICH station this is.
GENERIC_TOKENS = {
    'OF', 'THE', 'AND', 'AT', 'IN', 'ON', 'A', 'AN',
    'TRAVEL', 'CENTER', 'CENTRE', 'STOP', 'STOPS', 'TRUCK', 'TRUCKSTOP',
    'FUEL', 'FUELS', 'GAS', 'STATION', 'STORE', 'SHOP', 'MART', 'MINI',
    'INC', 'LLC', 'CO', 'COMPANY', 'USA', 'US', 'PLAZA', 'EXPRESS',
    'REST', 'AREA', 'WELCOME', 'SERVICE', 'SERVICES', 'FOOD', 'FOODS',
}


def _haversine_miles(lat1, lon1, lat2, lon2):
    import math
    R = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def name_tokens(name):
    """Distinctive tokens of a station/POI name (numbers and generic words dropped)."""
    name = re.sub(r'#\S*', ' ', (name or '').upper())     # drop branch numbers like #0370
    name = re.sub(r'[^A-Z0-9 ]', ' ', name)
    return {w for w in name.split() if len(w) > 1 and not w.isdigit() and w not in GENERIC_TOKENS}


def _clean_query_name(name):
    name = re.sub(r'#\S*', '', name or '')               # "#912" confuses search
    return re.sub(r'\s+', ' ', name).strip()


class StationResolver:
    def __init__(self, client, cache_path=None, verify_radius_mi=VERIFY_RADIUS_MI,
                 positive_ttl_days=POSITIVE_TTL_DAYS, negative_ttl_days=NEGATIVE_TTL_DAYS,
                 junction_index_path=None):
        self.client = client
        self.cache_path = cache_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'data', 'station_resolutions.json')
        self.junction_index_path = junction_index_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), 'data', 'exit_junctions.json')
        self._junction_index = None   # lazy
        self.verify_radius_mi = verify_radius_mi
        self.positive_ttl = positive_ttl_days * 86400
        self.negative_ttl = negative_ttl_days * 86400
        self._cache = self._load()
        self._last_call = 0.0
        self.calls_made = 0            # live lookups this process (evidence for the call budget)

    def _load(self):
        try:
            with open(self.cache_path) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save(self):
        tmp = self.cache_path + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(self._cache, f, indent=1, sort_keys=True)
        os.replace(tmp, self.cache_path)

    def _fresh(self, entry):
        ts = entry.get('ts', 0)
        ttl = self.positive_ttl if entry.get('resolved') else self.negative_ttl
        return (time.time() - ts) < ttl

    def resolve(self, station, allow_junction=False):
        """Verified {'lat','lon','source','evidence'} for this exact station, or None.

        station: FuelStation with truckstop_id, name, city, state and the
        city-level lat/lon used for proximity validation.

        Tier 1 (always): single Nominatim POI search with identity binding -
        this is the only tier that yields an ACTUAL station position.
        Tier 2 (only when allow_junction=True, i.e. the caller explicitly
        asked for the approximate mode): the exit junction named in the
        station's OWN CSV address, resolved against a prebuilt OSM
        motorway-junction index. An exit junction is NOT the station: it
        binds the vicinity, and the lot/entrance within ~0.5 mi is
        unverified. It exists only as an explicitly approximate fallback.
        """
        key = str(station.truckstop_id)
        entry = self._cache.get(key)
        if entry and self._fresh(entry):
            res = entry['result'] if entry.get('resolved') else None
            if res is None or allow_junction or res['source'] != 'csv_address_exit_junction':
                return res
            # strict caller: a cached junction-only result is not verification
            return None

        result, reason = self._lookup(station)
        if result is None and allow_junction:
            result, jreason = self._lookup_junction(station)
            reason = f"poi:{reason};junction:{jreason}"
        elif result is None:
            reason = f"poi:{reason}"
        self._cache[key] = {
            'ts': time.time(),
            'resolved': result is not None,
            'result': result,
            'reason': None if result is not None else reason,
        }
        self._save()
        return result

    def _lookup(self, station):
        """Tier 1: Nominatim POI search with identity binding. (result, reason)"""
        # One rate-limited, user-triggered search per uncached station.
        gap = 1.1 - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        self._last_call = time.time()
        self.calls_made += 1

        query = f"{_clean_query_name(station.name)}, {station.city}, {station.state}, USA"
        try:
            candidates = self.client.search_poi(query)
        except Exception:
            return None, 'lookup_error'
        if station.lat is None:
            return None, 'no_city_position'
        if not candidates:
            return None, 'no_candidates'

        want = name_tokens(station.name)
        matched = []
        for c in candidates:
            dist = _haversine_miles(station.lat, station.lon, c['lat'], c['lon'])
            if dist > self.verify_radius_mi:
                continue
            have = name_tokens(c['display_name'])
            shared = want & have
            if not shared:
                continue
            matched.append({**c, 'dist_mi': dist, 'shared': sorted(shared)})

        if not matched:
            return None, 'no_identity_match_in_radius'

        # Cluster candidates that sit at the same place; >1 cluster = ambiguous.
        matched.sort(key=lambda m: m['dist_mi'])
        clusters = []
        for m in matched:
            for cl in clusters:
                if _haversine_miles(cl[0]['lat'], cl[0]['lon'], m['lat'], m['lon']) <= SAME_PLACE_MI:
                    cl.append(m)
                    break
            else:
                clusters.append([m])
        if len(clusters) != 1:
            return None, 'ambiguous_multiple_clusters'

        best = clusters[0][0]
        return {
            'lat': best['lat'],
            'lon': best['lon'],
            'source': 'osm_nominatim_identity',
            'evidence': {
                'query': query,
                'display_name': best['display_name'],
                'osm_class': best['osm_class'],
                'osm_type': best['osm_type'],
                'osm_id': best['osm_id'],
                'shared_tokens': best['shared'],
                'distance_from_city_mi': round(best['dist_mi'], 2),
                'attribution': '(c) OpenStreetMap contributors, ODbL',
            },
        }, None

    def _load_junction_index(self):
        if self._junction_index is None:
            try:
                with open(self.junction_index_path) as f:
                    self._junction_index = json.load(f)
            except Exception:
                self._junction_index = {}
        return self._junction_index

    @staticmethod
    def _parse_address_exit(address):
        """(highways, exit) parsed from the station's own CSV address, or None.

        Handles 'I-80 & US-183, EXIT 257', 'I-80 Exit 360', 'US-80, EXIT 126'.
        """
        if not address:
            return None
        m = re.search(r'\bEX(?:IT)?\.?\s*(?:NO\.?\s*)?(\d+[A-Z]?)\b', address.upper())
        if not m:
            return None
        hwys = re.findall(r'\b(I|US|SR|SH|HWY|CR|RT|ST)[-\s]?(\d+[A-Z]?)\b', address.upper())
        if not hwys:
            return None
        norms = []
        for kind, num in hwys:
            kind = {'I': 'I', 'US': 'US', 'RT': 'US', 'ST': 'US'}.get(kind, kind)
            norms.append(f'{kind}{num}')
        return norms, m.group(1)

    def _lookup_junction(self, station):
        """Tier 2: offline exit-junction resolution bound to the CSV address.

        (result, reason). Zero external calls; the index is a prebuilt extract
        of OSM motorway_junction nodes (Geofabrik state extracts, ODbL).
        """
        parsed = self._parse_address_exit(station.address)
        if not parsed:
            return None, 'no_exit_in_address'
        index = self._load_junction_index()
        st = (station.state or '').lower()
        hwys, exit_num = parsed
        tried = []
        # No automatic highway correction: rewriting the address's highway
        # (e.g. 'US-80' -> 'I-80') would change identity on a guess.
        entry = None
        matched_hwy = None
        for h in hwys:
            key = f'{st}|{h}|{exit_num}'
            tried.append(key)
            if key in index:
                entry = index[key]
                matched_hwy = h
                break
        if entry is None:
            return None, 'exit_not_in_index' if any(
                k.startswith(f'{st}|') for k in index) else 'no_index_coverage'

        nodes = entry['nodes']
        lat = sum(n[0] for n in nodes) / len(nodes)
        lon = sum(n[1] for n in nodes) / len(nodes)
        # Ambiguity: the same exit number twice on the same highway, far apart.
        spread = max(
            (_haversine_miles(nodes[i][0], nodes[i][1], nodes[j][0], nodes[j][1])
             for i in range(len(nodes)) for j in range(i + 1, len(nodes))),
            default=0.0)
        if spread > JUNCTION_SPREAD_MI:
            return None, 'ambiguous_spread_out_junction'
        if station.lat is None:
            return None, 'no_city_position'
        dist = _haversine_miles(station.lat, station.lon, lat, lon)
        if dist > JUNCTION_RADIUS_MI:
            return None, 'junction_out_of_radius'

        return {
            'lat': lat,
            'lon': lon,
            'source': 'csv_address_exit_junction',
            'evidence': {
                'csv_address': station.address,
                'parsed_highway': matched_hwy,
                'parsed_exit': exit_num,
                'osm_junction_nodes': [n[2] for n in nodes],
                'node_spread_mi': round(spread, 2),
                'distance_from_city_mi': round(dist, 2),
                'caveat': ("position is the highway exit stated in the station's "
                           "own CSV address; the lot position within ~0.5 mi of "
                           "the junction is not separately verified"),
                'attribution': '(c) OpenStreetMap contributors, ODbL',
            },
        }, None
