"""
One-time preprocessing: resolve every CSV station's (city, state) to
coordinates and write router/data/station_locations.json.

Sources, in order:
1. US Census Gazetteer place file (public domain, offline data file):
   https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2025_Gazetteer/2025_Gaz_place_national.zip
2. Optional --nominatim-misses: a single one-time, single-threaded,
   rate-limited (>=1.1s gap), disk-cached pass over the remaining US misses.
   This follows the Nominatim usage policy's "smaller one-time bulk tasks"
   clause (single thread, one machine, cached results, identifying UA).
   Stations in Canada (the CSV includes some) are excluded: the assignment
   covers routes within the USA.

Coordinates are CITY-LEVEL centroids, because the CSV provides no street
geometry for its highway-exit addresses. The runtime API never geocodes
stations; it only reads the JSON this command produces.

Usage:
    python manage.py geocode_stations --gazetteer /path/to/2025_Gaz_place_national.txt
    python manage.py geocode_stations --gazetteer ... --nominatim-misses
"""

import csv
import json
import re
import time
from collections import defaultdict
from pathlib import Path

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

LSAD_SUFFIXES = re.compile(
    r'\s+(CDP|city|town|village|borough|municipality|charter township|township'
    r'|city and borough|consolidated government|metro government'
    r'|metropolitan government|unified government|corporation|comunidad'
    r'|zona urbana)$', re.I)
LSAD_PREFIXES = re.compile(
    r'^(TOWN OF|CITY OF|VILLAGE OF|BOROUGH OF|TOWNSHIP OF)\s+', re.I)


def norm(name: str) -> str:
    # Delegates to fuel_data.norm_place so CSV cities join with the keys this
    # command writes into station_locations.json.
    from router.fuel_data import norm_place
    return norm_place(name)


def load_gazetteer(path: Path):
    gaz = {}
    by_state = defaultdict(list)
    with open(path, encoding='utf-8') as f:
        for row in csv.DictReader(f, delimiter='|'):
            try:
                lat = float(row['INTPTLAT'])
                lon = float(row['INTPTLONG'])
                aland = float(row['ALAND'] or 0)
            except (ValueError, KeyError):
                continue
            key = (row['USPS'], norm(row['NAME']))
            gaz[key] = (lat, lon)
            by_state[row['USPS']].append((key[1], lat, lon, aland))
    return gaz, by_state


def gazetteer_lookup(state: str, city_norm: str, gaz, by_state):
    hit = gaz.get((state, city_norm))
    if hit:
        return hit
    entries = by_state.get(state, [])
    nospace = city_norm.replace(' ', '')
    cands = [e for e in entries if e[0].replace(' ', '') == nospace]
    if cands:
        cands.sort(key=lambda e: e[3])
        return (cands[0][1], cands[0][2])
    if len(city_norm) >= 4:
        cands = [e for e in entries if e[0].startswith(city_norm + ' ')]
        if cands:
            cands.sort(key=lambda e: (len(e[0]), e[3]))
            return (cands[0][1], cands[0][2])
    return None


class Command(BaseCommand):
    help = 'Resolve CSV station cities to coordinates (one-time preprocessing).'

    def add_arguments(self, parser):
        parser.add_argument('--gazetteer', required=True,
                            help='Path to the Census gazetteer place file (.txt).')
        parser.add_argument('--nominatim-misses', action='store_true',
                            help='One-time rate-limited Nominatim pass over US misses.')
        parser.add_argument('--output', default=None,
                            help='Output JSON path (default: router/data/station_locations.json).')
        parser.add_argument('--max-queries', type=int, default=0,
                            help='Cap Nominatim queries this run (0 = no cap). Existing output is reloaded and already-resolved cities are skipped, so chunked runs resume safely.')

    def handle(self, *args, **options):
        csv_path = Path(getattr(settings, 'FUEL_CSV_PATH'))
        if not csv_path.exists():
            raise CommandError(f'CSV not found at {csv_path}')
        gaz_path = Path(options['gazetteer'])
        if not gaz_path.exists():
            raise CommandError(f'Gazetteer file not found at {gaz_path}')
        out_path = Path(options['output']) if options['output'] else (
            Path(__file__).resolve().parent.parent.parent / 'data' / 'station_locations.json')

        # Resume support: preload previously written locations.
        locations = {}
        if out_path.exists():
            try:
                locations = json.loads(out_path.read_text(encoding='utf-8'))
            except (ValueError, OSError):
                locations = {}

        gaz, by_state = load_gazetteer(gaz_path)
        us_states = set(by_state)

        with open(csv_path, encoding='utf-8-sig', newline='') as f:
            rows = list(csv.DictReader(f))

        city_counts = {}
        for row in rows:
            key = (row['State'].strip().upper(), norm(row['City']))
            city_counts[key] = city_counts.get(key, 0) + 1

        misses = []
        for (state, city_norm), count in sorted(city_counts.items()):
            if f'{state}|{city_norm}' in locations:
                continue  # resolved in a previous run
            hit = gazetteer_lookup(state, city_norm, gaz, by_state)
            if hit:
                locations[f'{state}|{city_norm}'] = {
                    'lat': round(hit[0], 6), 'lon': round(hit[1], 6),
                    'precision': 'city_centroid', 'source': 'census_gazetteer',
                }
            else:
                misses.append((state, city_norm, count))

        non_us = [m for m in misses if m[0] not in us_states]
        us_misses = [m for m in misses if m[0] in us_states]

        if options['nominatim_misses'] and us_misses:
            session = requests.Session()
            session.headers.update({'User-Agent': getattr(
                settings, 'NOMINATIM_USER_AGENT', 'fuel-route-assessment/1.0')})
            base = getattr(settings, 'NOMINATIM_BASE_URL',
                           'https://nominatim.openstreetmap.org')
            state_names = {v: k.title() for k, v in _state_map().items()}
            still = []
            queries = 0
            max_queries = options['max_queries']
            for i, (state, city_norm, count) in enumerate(us_misses):
                if max_queries and queries >= max_queries:
                    still.append((state, city_norm, count))
                    continue
                if queries:
                    time.sleep(1.1)  # Nominatim policy: max 1 req/s
                queries += 1
                q = f"{city_norm.title()}, {state_names.get(state, state)}, USA"
                try:
                    resp = session.get(f'{base}/search', params={
                        'q': q, 'format': 'json', 'limit': 1,
                        'countrycodes': 'us'}, timeout=15)
                    resp.raise_for_status()
                    data = resp.json()
                except Exception:
                    data = []
                if data:
                    locations[f'{state}|{city_norm}'] = {
                        'lat': round(float(data[0]['lat']), 6),
                        'lon': round(float(data[0]['lon']), 6),
                        'precision': 'city_centroid', 'source': 'nominatim_one_time',
                    }
                    # Save after every result so interrupted runs keep progress.
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    out_path.write_text(json.dumps(locations, indent=1, sort_keys=True), encoding='utf-8')
                else:
                    still.append((state, city_norm, count))
            us_misses = still

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(locations, indent=1, sort_keys=True), encoding='utf-8')

        covered = sum(c for k, c in city_counts.items() if f'{k[0]}|{k[1]}' in locations)
        self.stdout.write(self.style.SUCCESS(
            f'Wrote {len(locations)} city locations to {out_path}\n'
            f'Station coverage: {covered}/{len(rows)} ({covered / len(rows):.1%})\n'
            f'Excluded non-US: {len(non_us)} cities / {sum(c for _, _, c in non_us)} stations\n'
            f'Unresolved US cities: {len(us_misses)} / {sum(c for _, _, c in us_misses)} stations'
        ))
        for state, city_norm, count in us_misses[:20]:
            self.stdout.write(f'  miss: {city_norm}, {state} ({count} stations)')


def _state_map():
    from router.routing import US_STATES
    return US_STATES
