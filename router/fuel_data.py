"""
Utility to load and query the fuel-price CSV efficiently.

The CSV (fuel-prices-for-be-assessment.csv) lists truck stops across the USA
with a retail price per gallon. It has no coordinates; each station is
joined to a CITY-LEVEL coordinate from router/data/station_locations.json
(produced once by `python manage.py geocode_stations`, see that command for
sources and policy notes). Stations without a coordinate stay usable for
price queries but are never proposed as fuel stops.

Loading is done once and cached in memory for fast lookups.
"""

import csv
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from django.conf import settings

_LSAD_SUFFIXES = re.compile(
    r'\s+(CDP|city|town|village|borough|municipality|charter township|township'
    r'|city and borough|consolidated government|metro government'
    r'|metropolitan government|unified government|corporation|comunidad'
    r'|zona urbana)$', re.I)
_LSAD_PREFIXES = re.compile(
    r'^(TOWN OF|CITY OF|VILLAGE OF|BOROUGH OF|TOWNSHIP OF)\s+', re.I)


def norm_place(name: str) -> str:
    """Normalize a place name the same way geocode_stations does, so CSV
    cities join with the station_locations.json keys."""
    n = name.strip().upper()
    n = _LSAD_PREFIXES.sub('', n)
    n = _LSAD_SUFFIXES.sub('', n)
    n = n.replace('.', '').replace("'", '')
    n = re.sub(r'\bSAINT\b', 'ST', n)
    n = re.sub(r'\bFORT\b', 'FT', n)
    n = re.sub(r'\bMOUNT\b', 'MT', n)
    return re.sub(r'\s+', ' ', n).strip()


@dataclass(frozen=True)
class FuelStation:
    truckstop_id: str
    name: str
    address: str
    city: str
    state: str
    retail_price: float
    lat: Optional[float] = None
    lon: Optional[float] = None
    location_precision: Optional[str] = None  # 'city_centroid' when located

    @property
    def key(self) -> tuple:
        return (self.state.strip().upper(), self.city.strip().lower())

    @property
    def located(self) -> bool:
        return self.lat is not None and self.lon is not None


def _default_locations_path() -> Path:
    return Path(__file__).resolve().parent / 'data' / 'station_locations.json'


class FuelPriceLoader:
    """
    Loads the fuel-price CSV once and provides efficient queries:

    * cheapest station in a given state
    * cheapest station in a given (state, city)
    * all stations in a state (sorted by price)
    * all stations that have coordinates (for stop planning)
    """

    def __init__(self, csv_path: Optional[Path] = None,
                 locations_path: Optional[Path] = None):
        self.csv_path = csv_path or getattr(settings, 'FUEL_CSV_PATH', None)
        self.locations_path = locations_path or getattr(
            settings, 'STATION_LOCATIONS_PATH', None) or _default_locations_path()
        self._stations: List[FuelStation] = []
        self._by_state: Dict[str, List[FuelStation]] = {}
        self._by_city: Dict[tuple, FuelStation] = {}  # cheapest per (state, city)
        self._loaded = False

    def _load_locations(self) -> Dict[str, dict]:
        path = Path(self.locations_path)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            return {}

    def load(self, force: bool = False) -> List[FuelStation]:
        """Load + index the CSV. Idempotent unless force=True."""
        if self._loaded and not force:
            return self._stations

        locations = self._load_locations()
        stations: List[FuelStation] = []
        path = self.csv_path
        if path is None or not Path(path).exists():
            raise FileNotFoundError(f"Fuel price CSV not found at {path}")

        with open(path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    price = float(row['Retail Price'])
                except (TypeError, ValueError):
                    continue
                state = str(row['State']).strip().upper()
                city = str(row['City']).strip()
                loc = locations.get(f'{state}|{norm_place(city)}')
                stations.append(FuelStation(
                    truckstop_id=str(row['OPIS Truckstop ID']).strip(),
                    name=str(row['Truckstop Name']).strip(),
                    address=str(row['Address']).strip(),
                    city=city,
                    state=state,
                    retail_price=price,
                    lat=loc['lat'] if loc else None,
                    lon=loc['lon'] if loc else None,
                    location_precision=loc['precision'] if loc else None,
                ))

        # Index: cheapest station per (state, city); all stations per state (price asc).
        by_state: Dict[str, List[FuelStation]] = {}
        by_city: Dict[tuple, FuelStation] = {}
        for s in stations:
            by_state.setdefault(s.state, []).append(s)
            k = s.key
            if k not in by_city or s.retail_price < by_city[k].retail_price:
                by_city[k] = s

        for state_stations in by_state.values():
            state_stations.sort(key=lambda x: x.retail_price)

        self._stations = stations
        self._by_state = by_state
        self._by_city = by_city
        self._loaded = True
        return stations

    def stations_in_state(self, state: str) -> List[FuelStation]:
        self.load()
        return self._by_state.get(state.strip().upper(), [])

    def cheapest_in_state(self, state: str) -> Optional[FuelStation]:
        """Return the cheapest station in a state, or None."""
        lst = self.stations_in_state(state)
        return lst[0] if lst else None

    def cheapest_in_city(self, state: str, city: str) -> Optional[FuelStation]:
        self.load()
        return self._by_city.get((state.strip().upper(), city.strip().lower()))

    def all_stations(self) -> List[FuelStation]:
        return self.load()

    def located_stations(self) -> List[FuelStation]:
        """Stations with coordinates, eligible for stop planning."""
        return [s for s in self.load() if s.located]

    def coverage(self) -> dict:
        stations = self.load()
        located = sum(1 for s in stations if s.located)
        from .routing import US_STATES
        us_codes = set(US_STATES.values())
        us = [s for s in stations if s.state in us_codes]
        us_located = sum(1 for s in us if s.located)
        return {
            'stations_total': len(stations),
            'stations_located': located,
            'us_stations': len(us),
            'us_located': us_located,
        }


# Module-level singleton so the CSV is parsed once per process.
loader = FuelPriceLoader()
