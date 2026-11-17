"""
Fuel-stop optimization over identity-verified station positions.

Strategy:

1. ONE OSRM route call -> geometry + cumulative route miles.
2. CANDIDATE FINDING (local data only): every located CSV station
   (city-level coordinates, see router/management/commands/geocode_stations.py)
   is mapped to its nearest route point: route mile + straight-line detour.
   Stations beyond STATION_CORRIDOR_MILES are ignored. City precision is
   enough to FIND candidates; it is never reported as a position.
3. Tank-aware greedy planning (assumed FULL TANK at mile 0, see note below):
   the implied tank capacity is range / mpg (e.g. 500 mi / 10 mpg = 50 gal).
   From the start, drive to the cheapest station reachable on the current
   tank; buy only enough to reach a cheaper station ahead, only what the
   final leg needs, or fill when nothing cheaper is reachable. Candidate
   legs use route miles + estimated detour (modeled distance).
4. VERIFICATION LOOP (plan_with_verification): every chosen stop must be
   identity-verified to an ACTUAL station position (router/resolver.py,
   strict mode) or it is excluded and the plan retried, bounded by
   max_attempts AND a global max_station_lookups cap on live lookups.
5. ROAD MEASUREMENT: ONE OSRM waypoint call (start + stops + finish)
   measures the finished plan on actual roads; purchases and range are
   RECOMPUTED on the road legs with the same tank rules. A road leg beyond
   the physical range triggers a bounded margin re-plan (which may repeat
   the waypoint call: at most 1 + len(margins) = 3 OSRM calls in total,
   always inside the brief's 'two or three acceptable' map/route budget).
6. If no reachable VERIFIED station chain exists, raise NoFeasiblePlan
   with the exact gap instead of inventing a stop.

Fuel semantics: the brief ("maximum range of 500 miles ... total money
spent on fuel") says nothing about the initial tank, so this project states
its assumption explicitly: the vehicle STARTS FULL, and its tank capacity
is the implied range / mpg (50 gal at 500 mi / 10 mpg). The planner tracks
the tank level physically: it never buys more than fits, buys only enough
to reach a cheaper station ahead when one exists, and buys only what the
final leg needs instead of filling up at the last stop. `total_fuel_cost_usd`
is money spent AT STOPS en route; `total_gallons_consumed` (distance / mpg)
is reported separately so a one-tank trip shows $0 purchased and the
gallons drawn from the starting tank.

Reported positions are identity-verified station positions (strict) or
explicitly-labelled exit-junction vicinity positions (approximate opt-in);
`road_detour_miles` is a HEURISTIC allocation of the waypoint route's extra
road distance (half of each adjacent leg's excess over the direct route
mileage attributed to the stop between them) - it is NOT a measured access
road to the lot.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from django.conf import settings

from .fuel_data import FuelStation, loader
from .routing import OSRMClient, RoutePoint, RouteResult


class NoFeasiblePlan(Exception):
    """Raised when no reachable station can complete the trip."""


@dataclass
class PlannedStop:
    order: int
    mile_marker: float                    # route mile nearest the station
    station: Optional[FuelStation]
    gallons: float                        # gallons purchased here
    cost_usd: float
    lat: Optional[float] = None           # station position (see location_precision)
    lon: Optional[float] = None
    detour_miles: Optional[float] = None  # straight-line (city level) or road-derived (verified)
    location_precision: Optional[str] = None
    road_detour_miles: Optional[float] = None   # road-derived extra miles (verified stops)
    position_source: Optional[str] = None       # e.g. osm_nominatim_identity


@dataclass
class FuelPlan:
    total_distance_miles: float
    total_gallons: float            # purchased en route
    total_gallons_consumed: float   # modeled distance / mpg (route + estimated detours)
    total_cost_usd: float           # money spent at stops
    mpg: float
    max_range_miles: float
    stops: List[PlannedStop] = field(default_factory=list)
    route: Optional[RouteResult] = None
    total_road_distance_miles: Optional[float] = None   # road-verified incl. detours (verified plans)
    verification_mode: str = 'strict'   # 'strict' = actual station positions only; 'approximate' allows exit-junction vicinity


def _haversine_miles(a: RoutePoint, b: RoutePoint) -> float:
    R = 3958.7613
    phi1, phi2 = math.radians(a.lat), math.radians(b.lat)
    dphi = math.radians(b.lat - a.lat)
    dlam = math.radians(b.lon - a.lon)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def _cumulative_miles(geometry: List[RoutePoint]) -> List[float]:
    cum = [0.0]
    for i in range(1, len(geometry)):
        cum.append(cum[-1] + _haversine_miles(geometry[i - 1], geometry[i]))
    return cum


def _nearest_route_mile(station: FuelStation, geometry: List[RoutePoint],
                        cum: List[float], coarse_idx: List[int]) -> Tuple[float, float]:
    """(route_mile, detour_miles) of the point on the route nearest a station.

    Coarse pass over a downsampled geometry, then a full-resolution refine
    around the best coarse point.
    """
    sp = RoutePoint(lat=station.lat, lon=station.lon)
    best_ci, best_d = None, float('inf')
    for ci in coarse_idx:
        d = _haversine_miles(sp, geometry[ci])
        if d < best_d:
            best_d, best_ci = d, ci
    # Refine: full-resolution window around the coarse hit.
    step = coarse_idx[1] - coarse_idx[0] if len(coarse_idx) > 1 else 1
    lo = max(0, best_ci - step)
    hi = min(len(geometry) - 1, best_ci + step)
    best_i, best_d = best_ci, best_d
    for i in range(lo, hi + 1):
        d = _haversine_miles(sp, geometry[i])
        if d < best_d:
            best_d, best_i = d, i
    return cum[best_i], best_d


class FuelOptimizer:
    def __init__(self, mpg: float = None, max_range_miles: float = None,
                 corridor_miles: float = None, client: Optional[OSRMClient] = None,
                 resolver=None):
        self.mpg = mpg if mpg is not None else float(getattr(settings, 'VEHICLE_MPG', 10))
        self.max_range_miles = max_range_miles if max_range_miles is not None else float(getattr(settings, 'VEHICLE_RANGE_MILES', 500))
        self.corridor_miles = corridor_miles if corridor_miles is not None else float(getattr(settings, 'STATION_CORRIDOR_MILES', 10))
        self._client = client or OSRMClient()
        self.resolver = resolver
        self._last_nominatim_call = 0.0

    def _nominatim_pause(self):
        import time
        min_gap = 1.1  # Nominatim policy: absolute max 1 req/s
        elapsed = time.time() - self._last_nominatim_call
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_nominatim_call = time.time()

    def plan(self, start: str, finish: str, mode: str = 'strict') -> FuelPlan:
        # 2 Nominatim geocodes (0 for lat,lng inputs) + 1 OSRM route call,
        # plus (verified mode) one cached lookup per candidate stop checked.
        self._nominatim_pause()
        start_point = self._client.geocode(start)
        self._nominatim_pause()
        finish_point = self._client.geocode(finish)
        route = self._client.route(start_point, finish_point)
        if self.resolver is not None:
            return self.plan_with_verification(route, self.resolver, mode=mode)
        return self.plan_from_route_result(route)

    def plan_with_verification(self, route: RouteResult, resolver,
                               max_attempts: int = 20, mode: str = 'strict',
                               max_station_lookups: int = 25) -> FuelPlan:
        """Plan, then make every chosen stop REAL:

        mode='strict' (default): only identity-verified ACTUAL station
        positions count; unresolved stops are excluded and re-planned, and
        if no verified reachable chain exists the call fails clearly (422).
        mode='approximate' (explicit opt-in): stations unresolved at POI
        level may fall back to their CSV-address exit junction (vicinity,
        not the lot) and every such stop is labelled
        position_source='csv_address_exit_junction'.


        1. Verify each chosen stop's identity + position via the resolver
           (one cached, user-triggered lookup per station). Stops that
           cannot be verified are excluded and the plan is retried -
           never silently kept at city precision.
        2. Measure the finished plan on ACTUAL ROADS with a single OSRM
           waypoint call; recompute purchases on the road legs (same
           tank rules) and report road-derived detours.
        3. If a road leg exceeds the physical tank range, re-plan with a
           reduced planning range (margin for detours), bounded.
        """
        rng = self.max_range_miles
        excluded = set()
        margins = [1.0, 0.92]   # bounded: keeps total OSRM calls <= 3 (brief budget)
        margin_i = 0
        allow_junction = mode == 'approximate'
        lookups_at_start = getattr(resolver, 'calls_made', 0)
        for _ in range(max_attempts):
            try:
                plan = self.plan_from_route_result(
                    route, excluded=excluded, effective_range=rng * margins[margin_i])
            except NoFeasiblePlan:
                if margin_i + 1 < len(margins):
                    margin_i += 1
                    continue
                raise
            if not plan.stops:
                return plan  # one-tank trip: nothing to verify, no waypoint call

            # Global live-lookup cap: max_attempts bounds loop iterations,
            # NOT lookups (each iteration can check several candidates).
            # Checked before EVERY resolve, so this cap is the actual bound
            # on station-lookup calls per request; results are cached, so a
            # retry continues cheaply from where it stopped.
            resolved = {}
            for s in plan.stops:
                if getattr(resolver, 'calls_made', 0) - lookups_at_start >= max_station_lookups:
                    raise NoFeasiblePlan(
                        f"Station-verification lookup budget ({max_station_lookups} "
                        "live lookups) exceeded on a cold cache for this corridor; "
                        "results are cached, so retry the same request to continue "
                        "from where it stopped.")
                resolved[s.station.truckstop_id] = resolver.resolve(
                    s.station, allow_junction=allow_junction)
            unverified = [tid for tid, r in resolved.items() if r is None]
            if unverified:
                excluded.update(unverified)
                continue

            points = [route.start] + [
                RoutePoint(lat=resolved[s.station.truckstop_id]['lat'],
                           lon=resolved[s.station.truckstop_id]['lon'])
                for s in plan.stops] + [route.finish]
            road = self._client.route_via(points)
            legs = road.legs_miles
            if any(leg > rng + 1e-6 for leg in legs):
                if margin_i + 1 < len(margins):
                    margin_i += 1
                    continue
                raise NoFeasiblePlan(
                    f"Road legs between the chosen stops exceed the {rng:.0f}-mile "
                    f"tank range even with a {margins[-1]:.0%} planning margin; "
                    f"no road-feasible plan with verifiable stations.")
            self._apply_road(plan, legs, resolved, road.distance_miles)
            plan.verification_mode = mode
            return plan
        raise NoFeasiblePlan(
            "No feasible plan: along this corridor there is no reachable "
            + ("chain of stations with identity-verified actual positions. "
               "Retry with mode='approximate' to permit explicitly-labelled "
               "exit-junction vicinity positions for unresolved stations."
               if not allow_junction else
               "chain of stations even permitting approximate exit-junction "
               "vicinity positions."))

    def _apply_road(self, plan: FuelPlan, legs: List[float], resolved: dict,
                    total_road_miles: float):
        """Replace estimates with road measurements and recompute purchases
        on the actual road legs (same tank rules as the planner)."""
        mpg, rng = self.mpg, self.max_range_miles
        capacity = rng / mpg
        n = len(plan.stops)

        for s in plan.stops:
            r = resolved[s.station.truckstop_id]
            s.lat, s.lon = r['lat'], r['lon']
            s.location_precision = r['source']
            s.position_source = r['source']

        # Road detour per stop: each stop's extra road miles appear in the
        # leg in and the leg out; attribute half of each adjacent extra.
        expected = []
        prev = 0.0
        for s in plan.stops:
            expected.append(s.mile_marker - prev)
            prev = s.mile_marker
        expected.append(plan.total_distance_miles - prev)
        extras = [max(0.0, legs[k] - expected[k]) for k in range(len(legs))]
        for i, s in enumerate(plan.stops):
            s.road_detour_miles = round((extras[i] + extras[i + 1]) / 2.0, 1)
            s.detour_miles = s.road_detour_miles  # verified plan: detour is road-derived

        # Purchases recomputed on road legs (legs[i] is the leg INTO stop i;
        # legs[n] is the final leg to the destination).
        tank = capacity
        for i, s in enumerate(plan.stops):
            tank -= legs[i] / mpg
            remaining = legs[i + 1:]
            target = None
            cum = 0.0
            for j in range(i + 1, n):
                cum += legs[j]
                if cum > rng + 1e-9:
                    break
                if plan.stops[j].station.retail_price < s.station.retail_price - 1e-12:
                    target = cum / mpg           # nearest cheaper station in reach
                    break
            if target is None:
                total_remaining = sum(remaining)
                target = total_remaining / mpg if total_remaining <= rng + 1e-9 else capacity
            buy = max(0.0, min(target, capacity) - tank)
            s.gallons = round(buy, 3)
            s.cost_usd = round(buy * s.station.retail_price, 2)
            tank += buy

        plan.total_gallons = round(sum(s.gallons for s in plan.stops), 3)
        plan.total_cost_usd = round(sum(s.cost_usd for s in plan.stops), 2)
        plan.total_road_distance_miles = round(total_road_miles, 2)
        plan.total_gallons_consumed = round(total_road_miles / mpg, 3)

    def plan_from_route_result(self, route: RouteResult, excluded=frozenset(),
                               effective_range: float = None) -> FuelPlan:
        geometry = route.geometry
        distance = route.distance_miles
        mpg, rng, corridor = self.mpg, self.max_range_miles, self.corridor_miles
        capacity = rng / mpg  # tank size implied by the brief's range + mpg (gal)
        er = effective_range or rng   # planning reach (may include a detour margin)

        cum = _cumulative_miles(geometry)
        # Downsample to ~1 point per 2 miles for the coarse pass.
        n = len(geometry)
        step = max(1, int(n * 2.0 / max(distance, 1.0)))
        coarse_idx = list(range(0, n, step))
        if coarse_idx[-1] != n - 1:
            coarse_idx.append(n - 1)

        # Map every located station to its nearest route point once.
        candidates: List[Tuple[float, float, FuelStation]] = []  # (route_mile, detour, station)
        for station in loader.located_stations():
            if excluded and station.truckstop_id in excluded:
                continue
            mile, detour = _nearest_route_mile(station, geometry, cum, coarse_idx)
            if detour <= corridor:
                candidates.append((mile, detour, station))
        candidates.sort(key=lambda c: c[0])

        stops: List[PlannedStop] = []
        tank = capacity   # gallons on board; starts full (stated assumption)
        position = 0.0    # route mile of the vehicle
        current: Optional[PlannedStop] = None   # stop we are parked at
        current_price: Optional[float] = None

        def leg(mile_to: float, detour_to: float) -> float:
            # Modeled leg distance: route miles + the destination stop's
            # estimated round-trip detour off the route and back.
            return (mile_to - position) + 2.0 * detour_to

        stop_detours: List[float] = []  # unrounded, for modeled consumption

        def add_stop(mile: float, detour: float, station: FuelStation) -> PlannedStop:
            stop_detours.append(detour)
            stop = PlannedStop(
                order=len(stops) + 1,
                mile_marker=round(mile, 2),
                station=station,
                gallons=0.0,  # decided when we leave this stop
                cost_usd=0.0,
                lat=station.lat,
                lon=station.lon,
                detour_miles=round(detour, 1),
                location_precision=station.location_precision,
            )
            stops.append(stop)
            return stop

        while True:
            dest_leg = distance - position
            if dest_leg <= tank * mpg + 1e-9:
                break  # destination reachable on the current tank
            ahead = [c for c in candidates if c[0] > position + 1e-6]

            if current is None:
                # Trip start: full (free) tank, no purchase possible here -
                # aim for the cheapest station the current tank can reach.
                reachable = [c for c in ahead if leg(c[0], c[1]) <= min(tank * mpg, er) + 1e-9]
                if not reachable:
                    raise NoFeasiblePlan(
                        f"No fuel station within {corridor:.0f} miles of the route "
                        f"between route mile {position:.0f} and {position + tank * mpg:.0f} "
                        f"(modeled distance incl. estimated detours); the vehicle's "
                        f"{rng:.0f}-mile range cannot be covered with the located "
                        f"CSV stations."
                    )
                mile, detour, station = min(
                    reachable, key=lambda c: (c[2].retail_price, -c[0]))
                tank -= leg(mile, detour) / mpg
                position = mile
                current = add_stop(mile, detour, station)
                current_price = station.retail_price
                continue

            # Parked at a stop: decide how much to buy here.
            within_full = [c for c in ahead if leg(c[0], c[1]) <= er + 1e-9]
            cheaper = [c for c in within_full
                       if c[2].retail_price < current_price - 1e-12]
            if cheaper:
                # Buy only enough to reach the nearest cheaper station.
                nxt = min(cheaper, key=lambda c: leg(c[0], c[1]))
                target_tank = leg(nxt[0], nxt[1]) / mpg
            elif dest_leg <= rng + 1e-9:
                # No cheaper station ahead: buy only what the final leg needs.
                nxt = None
                target_tank = dest_leg / mpg
            elif within_full:
                # Nothing cheaper in reach: fill up, go to the cheapest
                # station a full tank can reach (ties: farthest along).
                nxt = min(within_full, key=lambda c: (c[2].retail_price, -c[0]))
                target_tank = capacity
            else:
                raise NoFeasiblePlan(
                    f"No fuel station within {corridor:.0f} miles of the route is "
                    f"reachable on a full {rng:.0f}-mile tank from route mile "
                    f"{position:.0f} (modeled distance incl. estimated detours), "
                    f"and the destination is still {dest_leg:.0f} route miles away."
                )

            buy = max(0.0, min(target_tank, capacity) - tank)
            current.gallons = round(buy, 3)
            current.cost_usd = round(buy * current_price, 2)
            tank += buy
            if nxt is None:
                break  # fueled for the final leg
            tank -= leg(nxt[0], nxt[1]) / mpg
            position = nxt[0]
            current = add_stop(nxt[0], nxt[1], nxt[2])
            current_price = nxt[2].retail_price

        total_gallons = sum(s.gallons for s in stops)
        total_cost = sum(s.cost_usd for s in stops)
        # Modeled consumption uses the same UNROUNDED detours as the tank
        # math; only the reported value is rounded.
        modeled_total = distance + 2.0 * sum(stop_detours)

        return FuelPlan(
            total_distance_miles=round(distance, 2),
            total_gallons=round(total_gallons, 3),
            total_gallons_consumed=round(modeled_total / mpg, 3),
            total_cost_usd=round(total_cost, 2),
            mpg=mpg,
            max_range_miles=rng,
            stops=stops,
            route=route,
        )
