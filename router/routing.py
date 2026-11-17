"""
Free routing/geocoding layer.

- OSRM (router.project-osrm.org): ONE call returns route geometry + distance.
- Nominatim: geocode start/finish (2 calls) and reverse-geocode a few route
  points to learn which states the route crosses (kept to a bare minimum).

No API keys required. Respects Nominatim's usage policy (1 req/s, custom UA).
"""

import math
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import requests

from django.conf import settings


class RoutingError(Exception):
    pass


@dataclass
class RoutePoint:
    lat: float
    lon: float

    @property
    def as_osrm(self) -> str:
        return f"{self.lon},{self.lat}"


@dataclass
class RouteResult:
    start: RoutePoint
    finish: RoutePoint
    distance_miles: float
    distance_km: float
    duration_seconds: float
    geometry: List[RoutePoint]          # full OSRM geometry (lon,lat) -> (lat,lon)
    summary: str
    legs_miles: Optional[List[float]] = None   # per-waypoint-leg distances (route_via)


class OSRMClient:
    def __init__(self):
        self.base = getattr(settings, 'OSRM_BASE_URL', 'https://router.project-osrm.org').rstrip('/')
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': getattr(settings, 'NOMINATIM_USER_AGENT', 'fuel-route/1.0')})

    def geocode(self, location: str) -> RoutePoint:
        """Turn 'city,state' or 'lat,lng' into a RoutePoint via Nominatim."""
        location = location.strip()

        # Already coordinates?
        if ',' in location:
            parts = [p.strip() for p in location.split(',')]
            if len(parts) == 2:
                try:
                    lat = float(parts[0])
                    lon = float(parts[1])
                    return RoutePoint(lat=lat, lon=lon)
                except ValueError:
                    pass

        url = f"{getattr(settings, 'NOMINATIM_BASE_URL', 'https://nominatim.openstreetmap.org')}/search"
        params = {
            'q': location,
            'format': 'json',
            'limit': 1,
            'addressdetails': 1,
        }
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data:
            raise RoutingError(f"Could not geocode location: {location}")
        top = data[0]
        lat = float(top['lat'])
        lon = float(top['lon'])
        return RoutePoint(lat=lat, lon=lon)

    def reverse_geocode(self, lat: float, lon: float) -> Optional[dict]:
        """Reverse-geocode a point (used sparingly to learn route states)."""
        url = f"{getattr(settings, 'NOMINATIM_BASE_URL', 'https://nominatim.openstreetmap.org')}/reverse"
        params = {'lat': lat, 'lon': lon, 'format': 'json', 'zoom': 5, 'addressdetails': 1}
        try:
            resp = self.session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return None

    def search_poi(self, query: str, limit: int = 5) -> List[dict]:
        """Nominatim search returning raw candidates for identity matching.

        Used ONLY for user-triggered verification of chosen fuel stops
        (Nominatim policy: end-user-triggered app use, max 1 req/s, results
        cached - see router/resolver.py). Never used for bulk lookups.
        """
        url = f"{getattr(settings, 'NOMINATIM_BASE_URL', 'https://nominatim.openstreetmap.org')}/search"
        params = {'q': query, 'format': 'json', 'limit': limit, 'addressdetails': 1}
        resp = self.session.get(url, params=params, timeout=15)
        resp.raise_for_status()
        out = []
        for item in resp.json():
            out.append({
                'lat': float(item['lat']),
                'lon': float(item['lon']),
                'display_name': item.get('display_name', ''),
                'osm_type': item.get('type', ''),
                'osm_class': item.get('class', ''),
                'osm_id': item.get('osm_id'),
            })
        return out

    def route(self, start: RoutePoint, finish: RoutePoint) -> RouteResult:
        """Single OSRM call: driving route with full geometry."""
        coords = f"{start.as_osrm};{finish.as_osrm}"
        url = f"{self.base}/route/v1/driving/{coords}"
        params = {
            'overview': 'full',
            'geometries': 'geojson',
            'steps': 'false',
        }
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 'Ok' or not data.get('routes'):
            raise RoutingError(f"OSRM returned no route: {data.get('message')}")

        route = data['routes'][0]
        leg = route['legs'][0]
        distance_meters = leg['distance']          # meters
        duration = leg['duration']                 # seconds

        # geometry comes as [[lon, lat], ...] -> convert to RoutePoint(lat, lon)
        raw_geom = route['geometry']['coordinates']
        geometry = [RoutePoint(lat=p[1], lon=p[0]) for p in raw_geom]

        return RouteResult(
            start=start,
            finish=finish,
            distance_miles=distance_meters / 1609.344,
            distance_km=distance_meters / 1000.0,
            duration_seconds=duration,
            geometry=geometry,
            summary=leg.get('summary', ''),
        )
    def route_via(self, points: List[RoutePoint]) -> RouteResult:
        """Single OSRM call through ordered waypoints (start, stops, finish).

        Returns the overall route plus per-leg road distances, used to
        measure the ACTUAL road mileage of a finished plan (detours included).
        NOTE: public OSRM profiles car routing; it is not a truck-access
        guarantee.
        """
        coords = ';'.join(p.as_osrm for p in points)
        url = f"{self.base}/route/v1/driving/{coords}"
        params = {'overview': 'false', 'geometries': 'geojson', 'steps': 'false'}
        resp = self.session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get('code') != 'Ok' or not data.get('routes'):
            raise RoutingError(f"OSRM returned no waypoint route: {data.get('message')}")
        route = data['routes'][0]
        legs = [leg['distance'] / 1609.344 for leg in route['legs']]
        return RouteResult(
            start=points[0],
            finish=points[-1],
            distance_miles=sum(legs),
            distance_km=sum(legs) * 1.609344,
            duration_seconds=route.get('duration', 0.0),
            geometry=[],
            summary='waypoint-road-check',
            legs_miles=legs,
        )


# US state full name / ISO code -> 2-letter abbreviation lookup.
US_STATES = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT',
    'delaware': 'DE', 'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI',
    'idaho': 'ID', 'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA',
    'kansas': 'KS', 'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME',
    'maryland': 'MD', 'massachusetts': 'MA', 'michigan': 'MI',
    'minnesota': 'MN', 'mississippi': 'MS', 'missouri': 'MO',
    'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM',
    'new york': 'NY', 'north carolina': 'NC', 'north dakota': 'ND',
    'ohio': 'OH', 'oklahoma': 'OK', 'oregon': 'OR',
    'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX',
    'utah': 'UT', 'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA',
    'west virginia': 'WV', 'wisconsin': 'WI', 'wyoming': 'WY',
    'district of columbia': 'DC',
}


def normalize_state(raw: Optional[str]) -> Optional[str]:
    """Convert a Nominatim state (full name or US-CO ISO code) to a 2-letter code."""
    if not raw:
        return None
    raw = raw.strip()
    # ISO3166-2 style: US-CO
    if '-' in raw:
        return raw.split('-')[-1].upper()
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return US_STATES.get(raw.lower())


# Haversine for measuring distance along the route geometry.
def _haversine_miles(p1: RoutePoint, p2: RoutePoint) -> float:
    R = 3958.7613  # miles
    phi1, phi2 = math.radians(p1.lat), math.radians(p2.lat)
    dphi = math.radians(p2.lat - p1.lat)
    dlam = math.radians(p2.lon - p1.lon)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def interpolate_point(geometry: List[RoutePoint], target_mile: float) -> Optional[RoutePoint]:
    """
    Return the (lat, lon) on the route at `target_mile` miles from the start,
    by walking the OSRM geometry with cumulative haversine distance.
    """
    if not geometry:
        return None
    if target_mile <= 0:
        return geometry[0]

    cumulative = 0.0
    for i in range(1, len(geometry)):
        seg = _haversine_miles(geometry[i - 1], geometry[i])
        if cumulative + seg >= target_mile:
            if seg == 0:
                return geometry[i]
            frac = (target_mile - cumulative) / seg
            lat = geometry[i - 1].lat + (geometry[i].lat - geometry[i - 1].lat) * frac
            lon = geometry[i - 1].lon + (geometry[i].lon - geometry[i - 1].lon) * frac
            return RoutePoint(lat=lat, lon=lon)
        cumulative += seg
    return geometry[-1]
