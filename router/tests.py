"""
Tests for the fuel-route API.

All network access (OSRM, Nominatim) is mocked, so the suite runs fully
offline and never hammers the free public services. Station locations come
from the shipped router/data/station_locations.json, or from synthetic
stations injected via the loader for planner tests.
"""

import dataclasses as _dc
import json
import tempfile
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from .fuel_data import loader, FuelStation, FuelPriceLoader
from .fuel_optimizer import (
    FuelOptimizer, FuelPlan, PlannedStop, NoFeasiblePlan, _cumulative_miles,
)
from .routing import RoutePoint, RouteResult, normalize_state


def make_station(state='NE', price=2.5, name='TEST STOP', city='Testville',
                 lat=None, lon=None, precision='city_centroid'):
    return FuelStation(
        truckstop_id='1', name=name, address='1 Test Rd',
        city=city, state=state, retail_price=price,
        lat=lat, lon=lon, location_precision=precision if lat is not None else None,
    )


def make_route(distance_miles, n_points=401, lat0=40.0, lon0=-95.0):
    """Straight-line fake route heading north: geometry + cumulative miles
    consistent enough for the range-placement logic."""
    total_deg = distance_miles / 69.0
    geometry = [
        RoutePoint(lat=lat0 + total_deg * i / (n_points - 1), lon=lon0)
        for i in range(n_points)
    ]
    return RouteResult(
        start=geometry[0],
        finish=geometry[-1],
        distance_miles=distance_miles,
        distance_km=distance_miles * 1.609344,
        duration_seconds=distance_miles * 60,
        geometry=geometry,
        summary='fake',
    )


def station_on_route(route, mile, price, name, offset_deg=0.0):
    """A station exactly `mile` miles along the fake north-south route.
    offset_deg shifts it EAST in longitude, i.e. genuinely off the route."""
    lat = route.start.lat + mile / 69.0
    return make_station(price=price, name=name, lat=lat, lon=route.start.lon + offset_deg)


class FuelLoaderTests(TestCase):
    def test_cheapest_in_state(self):
        loader.load()
        tx = loader.cheapest_in_state('TX')
        self.assertIsNotNone(tx)
        self.assertEqual(tx.state, 'TX')
        all_tx = loader.stations_in_state('TX')
        self.assertLessEqual(tx.retail_price, min(s.retail_price for s in all_tx))

    def test_state_lookup_is_case_insensitive(self):
        loader.load()
        self.assertEqual(loader.cheapest_in_state('tx'), loader.cheapest_in_state('TX'))

    def test_most_us_stations_have_locations(self):
        cov = loader.coverage()
        self.assertGreater(cov['stations_total'], 8000)
        # Preprocessing resolves essentially every US station's city.
        self.assertGreater(cov['us_located'] / cov['us_stations'], 0.95)

    def test_station_join_uses_normalized_names(self):
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump({'TX|ST PETERS': {'lat': 1.0, 'lon': 2.0,
                                        'precision': 'city_centroid',
                                        'source': 'test'}}, f)
            path = f.name
        ldr = FuelPriceLoader(locations_path=path)
        stations = ldr.load()
        # Only rows whose normalized (state, city) matches the join key may
        # come back located, and a located row must carry real coordinates.
        from .fuel_data import norm_place
        located = [s for s in stations if s.located]
        for s in located:
            self.assertIsNotNone(s.lat)
            self.assertIsNotNone(s.lon)
            self.assertEqual(s.state, 'TX')
            self.assertEqual(norm_place(s.city), 'ST PETERS')
        # Direct unit check of the join key:
        self.assertEqual(norm_place("St. Peters"), 'ST PETERS')
        self.assertEqual(norm_place("Town of Pecos city"), 'PECOS')
        self.assertEqual(norm_place("O'Neill"), 'ONEILL')


class NormalizeStateTests(TestCase):
    def test_full_name(self):
        self.assertEqual(normalize_state('Colorado'), 'CO')

    def test_iso_code(self):
        self.assertEqual(normalize_state('US-NE'), 'NE')

    def test_two_letter(self):
        self.assertEqual(normalize_state('tx'), 'TX')

    def test_unknown(self):
        self.assertIsNone(normalize_state('Atlantis'))
        self.assertIsNone(normalize_state(None))


class CumulativeMilesTests(TestCase):
    def test_monotonic_and_total(self):
        route = make_route(690.0)  # 10 degrees latitude ~ 690 miles
        cum = _cumulative_miles(route.geometry)
        self.assertEqual(cum[0], 0.0)
        self.assertTrue(all(b >= a for a, b in zip(cum, cum[1:])))
        self.assertAlmostEqual(cum[-1], 690.0, delta=5.0)


class OptimizerTests(TestCase):
    """Planner tests inject synthetic located stations through the loader."""

    def _run(self, route, stations, mpg=10, rng=500, corridor=10):
        opt = FuelOptimizer(mpg=mpg, max_range_miles=rng, corridor_miles=corridor)
        with patch('router.fuel_optimizer.loader') as mock_loader:
            mock_loader.located_stations.return_value = stations
            return opt.plan_from_route_result(route)

    def test_cheapest_reachable_station_wins(self):
        route = make_route(900.0)
        expensive = station_on_route(route, 300, 3.50, 'EXPENSIVE EARLY')
        cheap = station_on_route(route, 480, 2.50, 'CHEAP LATE')
        plan = self._run(route, [expensive, cheap])
        self.assertEqual(len(plan.stops), 1)
        self.assertEqual(plan.stops[0].station.name, 'CHEAP LATE')
        self.assertAlmostEqual(plan.stops[0].mile_marker, 480.0, delta=2.0)

    def test_station_beyond_first_tank_is_used_later_when_cheaper(self):
        route = make_route(900.0)
        far = station_on_route(route, 600, 2.00, 'CHEAP FAR')   # beyond the first tank
        near = station_on_route(route, 400, 3.00, 'PRICEY NEAR')
        plan = self._run(route, [far, near])
        # NEAR is unavoidable, but with a cheaper station 200 mi ahead the
        # planner buys only enough there to reach it, then uses the cheap one.
        self.assertEqual([s.station.name for s in plan.stops],
                         ['PRICEY NEAR', 'CHEAP FAR'])
        self.assertLess(plan.stops[0].gallons, 20.0)  # ~10 gal, not a fill-up
        self._assert_physical_tank(plan)

    def test_truly_unreachable_station_means_infeasible(self):
        route = make_route(900.0)
        far = station_on_route(route, 600, 2.00, 'TOO FAR')  # nothing in first-tank range
        with self.assertRaises(NoFeasiblePlan):
            self._run(route, [far])

    def test_station_outside_corridor_ignored(self):
        route = make_route(900.0)
        off = station_on_route(route, 400, 2.00, 'OFF ROUTE', offset_deg=1.0)  # ~69 mi away
        on = station_on_route(route, 450, 3.00, 'ON ROUTE')
        plan = self._run(route, [off, on])
        self.assertEqual([s.station.name for s in plan.stops], ['ON ROUTE'])
        self.assertLess(plan.stops[0].detour_miles, 1.0)

    def test_no_station_means_explicit_infeasible(self):
        route = make_route(900.0)
        with self.assertRaises(NoFeasiblePlan) as ctx:
            self._run(route, [])
        self.assertIn('500', str(ctx.exception))

    def _assert_physical_tank(self, plan, mpg=10, rng=500):
        """Replay the plan against a physical tank: never runs dry between
        stops (modeled miles incl. detours), never buys more than fits.
        PlannedStop fields are rounded (mile 2dp, detour 1dp, gallons 3dp),
        so the replay allows 0.05 gal of slack per leg."""
        capacity = rng / mpg
        tol = 0.05
        tank = capacity
        prev_mile = 0.0
        for s in plan.stops:
            leg_mi = (s.mile_marker - prev_mile) + 2.0 * (s.detour_miles or 0.0)
            tank -= leg_mi / mpg
            self.assertGreaterEqual(tank, -tol,
                                    f"ran dry before {s.station.name}")
            self.assertLessEqual(s.gallons, capacity - tank + tol,
                                 f"overfilled the {capacity:.0f}-gal tank at {s.station.name}")
            tank += s.gallons
            prev_mile = s.mile_marker
        final_leg = plan.total_distance_miles - prev_mile
        self.assertGreaterEqual(tank - final_leg / mpg, -tol,
                                "ran dry before the destination")

    def test_multi_stop_partial_fill_to_reach_cheaper(self):
        route = make_route(1200.0)
        s1 = station_on_route(route, 400, 3.00, 'STOP A')
        s2 = station_on_route(route, 850, 2.50, 'STOP B')
        plan = self._run(route, [s1, s2])
        self.assertEqual(len(plan.stops), 2)
        a, b = plan.stops
        # The implied tank holds 50 gal, so arrival at A leaves ~10. A buys
        # only what is needed to reach cheaper B (~35 gal) - buying for the
        # whole ~450-mi leg (~45 gal) would not fit in the tank. B buys the
        # final leg. (Synthetic-route miles are approximate; assert loosely.)
        self.assertAlmostEqual(a.gallons, 35.0, delta=1.5)
        self.assertLess(a.gallons, 40.0)  # a whole-leg buy would not fit
        self.assertAlmostEqual(b.gallons, 35.0, delta=1.5)
        self.assertAlmostEqual(plan.total_gallons, 70.0, delta=3.0)
        self.assertAlmostEqual(plan.total_gallons_consumed, 120.0, delta=1.0)
        self.assertAlmostEqual(plan.total_cost_usd,
                               a.gallons * 3.00 + b.gallons * 2.50, delta=0.05)
        self._assert_physical_tank(plan)

    def test_never_overfills_tank(self):
        route = make_route(1200.0)
        early = station_on_route(route, 120, 3.00, 'EARLY')
        mid = station_on_route(route, 560, 3.60, 'MID')
        late = station_on_route(route, 1000, 3.00, 'LATE')
        plan = self._run(route, [early, mid, late])
        self._assert_physical_tank(plan)
        # Arrival at EARLY leaves 38 gal; buying for the whole 440-mi leg to
        # MID would put 82 gal in a 50-gal tank. MID is more expensive and
        # nothing cheaper is in reach, so the planner tops up fully: 12 gal.
        self.assertAlmostEqual(plan.stops[0].gallons, 12.0, places=1)
        self.assertEqual([s.station.name for s in plan.stops],
                         ['EARLY', 'MID', 'LATE'])

    def test_detour_counts_against_range(self):
        route = make_route(900.0)
        # ~7 mi east of the route (0.15 deg lon at these latitudes).
        just_out = station_on_route(route, 490, 2.00, 'JUST OUT', offset_deg=0.15)
        # 490 route miles + ~14 mi round-trip detour > the 500-mi tank: the
        # station is not reachable and the plan must say so, not strand.
        with self.assertRaises(NoFeasiblePlan):
            self._run(route, [just_out])
        just_in = station_on_route(route, 480, 2.00, 'JUST IN', offset_deg=0.15)
        plan = self._run(route, [just_in])
        self.assertEqual([s.station.name for s in plan.stops], ['JUST IN'])
        self._assert_physical_tank(plan)

    def test_consumption_uses_unrounded_modeled_distance(self):
        route = make_route(1200.0)
        s1 = station_on_route(route, 400, 3.00, 'STOP A')
        s2 = station_on_route(route, 850, 2.50, 'STOP B')
        plan = self._run(route, [s1, s2])
        # Recompute the detours exactly as the planner sees them (unrounded)
        # and check consumption matches that modeled distance, not the 1-dp
        # detour_miles shown in the output.
        from .fuel_optimizer import _nearest_route_mile
        cum = _cumulative_miles(route.geometry)
        n = len(route.geometry)
        step = max(1, int(n * 2.0 / max(route.distance_miles, 1.0)))
        coarse_idx = list(range(0, n, step))
        if coarse_idx[-1] != n - 1:
            coarse_idx.append(n - 1)
        detours = [_nearest_route_mile(st, route.geometry, cum, coarse_idx)[1]
                   for st in (s1, s2)]
        modeled = route.distance_miles + 2.0 * sum(detours)
        self.assertAlmostEqual(plan.total_gallons_consumed * 10, modeled, places=2)

    def test_last_stop_buys_only_what_the_final_leg_needs(self):
        route = make_route(700.0)
        only = station_on_route(route, 400, 3.00, 'ONLY STOP')
        plan = self._run(route, [only])
        self.assertEqual(len(plan.stops), 1)
        # ~10 gal remain at mile 400; the ~300-mi final leg needs ~30, so the
        # stop buys ~20 - not a full ~40-gal top-up.
        self.assertAlmostEqual(plan.stops[0].gallons, 20.0, delta=1.5)
        self.assertLess(plan.stops[0].gallons, 25.0)  # a fill-up would be ~40
        self._assert_physical_tank(plan)

    def test_short_trip_no_stops_but_consumption_reported(self):
        route = make_route(300.0)
        plan = self._run(route, [])
        self.assertEqual(plan.stops, [])
        self.assertEqual(plan.total_gallons, 0.0)
        self.assertEqual(plan.total_cost_usd, 0.0)
        self.assertAlmostEqual(plan.total_gallons_consumed, 30.0, places=1)

    def test_exact_range_boundary_needs_no_stop(self):
        route = make_route(500.0)
        plan = self._run(route, [])
        self.assertEqual(plan.stops, [])

    def test_route_carried_for_map(self):
        route = make_route(300.0)
        plan = self._run(route, [])
        self.assertIs(plan.route, route)


class APITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.url = reverse('router:route-optimize')
        self._tmp_media = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp_media.cleanup)
        self._media_override = override_settings(MEDIA_ROOT=self._tmp_media.name)
        self._media_override.enable()
        self.addCleanup(self._media_override.disable)

    def _fake_plan(self, with_stop=True):
        route = make_route(800.0)
        stops = []
        if with_stop:
            st = make_station(price=2.5, lat=47.2, lon=-95.0)
            stops = [PlannedStop(
                order=1, mile_marker=500.0, station=st,
                gallons=30.0, cost_usd=75.0, lat=47.2, lon=-95.0,
                detour_miles=0.4, location_precision='city_centroid',
            )]
        return FuelPlan(
            total_distance_miles=800.0,
            total_gallons=30.0,
            total_gallons_consumed=80.0,
            total_cost_usd=75.0,
            mpg=10.0,
            max_range_miles=500.0,
            stops=stops,
            route=route,
        )

    def test_post_route_happy_path(self):
        with patch('router.views.FuelOptimizer.plan', return_value=self._fake_plan()) as mock_plan:
            resp = self.client.post(self.url, {'start': 'Chicago, IL', 'finish': 'Denver, CO'}, format='json')
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data['total_distance_miles'], 800.0)
        self.assertEqual(data['total_gallons_consumed'], 80.0)
        self.assertEqual(len(data['stops']), 1)
        self.assertEqual(data['stops'][0]['name'], 'TEST STOP')
        self.assertEqual(data['stops'][0]['location_precision'], 'city_centroid')
        self.assertTrue(data['map_url'].startswith('/media/route_'))
        mock_plan.assert_called_once_with('Chicago, IL', 'Denver, CO', mode='strict')

    def test_post_route_persists_and_is_retrievable(self):
        with patch('router.views.FuelOptimizer.plan', return_value=self._fake_plan()):
            resp = self.client.post(self.url, {'start': 'A', 'finish': 'B'}, format='json')
        pk = resp.json()['id']
        detail = self.client.get(reverse('router:route-detail', args=[pk]))
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['id'], pk)
        self.assertEqual(len(detail.json()['stops']), 1)

    def test_post_route_empty_input_rejected(self):
        resp = self.client.post(self.url, {'start': '', 'finish': 'Denver, CO'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_post_route_missing_field_rejected(self):
        resp = self.client.post(self.url, {'start': 'Chicago, IL'}, format='json')
        self.assertEqual(resp.status_code, 400)

    def test_post_route_infeasible_is_422_not_fabricated(self):
        with patch('router.views.FuelOptimizer.plan',
                   side_effect=NoFeasiblePlan('no station between mile 0 and 500')):
            resp = self.client.post(self.url, {'start': 'A', 'finish': 'B'}, format='json')
        self.assertEqual(resp.status_code, 422)
        self.assertEqual(resp.json()['error'], 'no_feasible_plan')

    def test_post_route_routing_failure_is_502(self):
        with patch('router.views.FuelOptimizer.plan', side_effect=Exception('no route')):
            resp = self.client.post(self.url, {'start': 'Nowhere', 'finish': 'Elsewhere'}, format='json')
        self.assertEqual(resp.status_code, 502)

    def test_route_detail_404(self):
        resp = self.client.get(reverse('router:route-detail', args=[99999]))
        self.assertEqual(resp.status_code, 404)

    def test_fuel_price_endpoint_shape(self):
        loader.load()
        tx = loader.cheapest_in_state('TX')
        if tx is None:
            self.skipTest('No TX stations in CSV')
        resp = self.client.get(reverse('router:fuel-price') + '?state=TX')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('retail_price', resp.json())

    def test_fuel_price_requires_state(self):
        resp = self.client.get(reverse('router:fuel-price'))
        self.assertEqual(resp.status_code, 400)

    def test_fuel_price_unknown_state_404(self):
        loader.load()
        resp = self.client.get(reverse('router:fuel-price') + '?state=ZZ')
        self.assertEqual(resp.status_code, 404)


class ExternalCallBudgetTests(TestCase):
    """Full accounting of EVERY external call a POST makes (all services).

    Verified architecture: 1 OSRM route + 1 OSRM waypoint road-check +
    <=2 Nominatim endpoint geocodes +, on FIRST-TIME corridors only, one
    cached Nominatim identity lookup per chosen stop (0 with warm cache).
    """

    def _post_with_optimizer(self, opt):
        client = APIClient()
        url = reverse('router:route-optimize')
        with patch.object(FuelOptimizer, '_nominatim_pause', lambda self: None), \
             patch('router.fuel_optimizer.loader') as mock_loader, \
             patch('router.views.FuelOptimizer', return_value=opt), \
             patch('router.views.StationResolver', return_value=opt.resolver):
            mock_loader.located_stations.return_value = self._stations
            resp = client.post(url, {'start': 'Chicago, IL', 'finish': 'Denver, CO'}, format='json')
        return resp

    def _setup_mocks(self, seed_cache):
        import tempfile, os, time as _time
        from .resolver import StationResolver
        route = make_route(800.0)
        station = station_on_route(route, 400, 2.5, 'MIDWAY KUM STOP')
        self._stations = [station]
        with patch('router.fuel_optimizer.OSRMClient') as mock_client_cls:
            opt = FuelOptimizer(mpg=10, max_range_miles=500)
            mock_client = mock_client_cls.return_value
            mock_client.geocode.side_effect = [
                RoutePoint(lat=41.8781, lon=-87.6298),
                RoutePoint(lat=39.7392, lon=-104.9903),
            ]
            mock_client.route.return_value = route
            mock_client.route_via.return_value = _road_result([405.0, 405.0])
            mock_client.search_poi.return_value = [{
                'lat': station.lat, 'lon': station.lon,
                'display_name': 'Midway Kum Stop, Frontage Rd, Testville, Nebraska, USA',
                'osm_class': 'amenity', 'osm_type': 'fuel', 'osm_id': 42,
            }]
            path = os.path.join(tempfile.mkdtemp(), 'res.json')
            if seed_cache:
                import json as _json
                with open(path, 'w') as f:
                    _json.dump({'1': {'ts': _time.time(), 'resolved': True,
                                      'result': _verified(station.lat, station.lon)}}, f)
            resolver = StationResolver(client=mock_client, cache_path=path)
            resolver._last_call = _time.time()
            opt.resolver = resolver
            resp = self._post_with_optimizer(opt)
        return resp, mock_client, resolver

    def test_warm_cache_call_accounting(self):
        resp, mock_client, resolver = self._setup_mocks(seed_cache=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_client.route.call_count, 1)        # main route
        self.assertEqual(mock_client.route_via.call_count, 1)    # road check
        self.assertEqual(mock_client.geocode.call_count, 2)      # endpoints
        mock_client.search_poi.assert_not_called()               # cache warm
        mock_client.reverse_geocode.assert_not_called()
        # Response shows the verified architecture.
        data = resp.json()
        self.assertEqual(data['stops'][0]['position_source'], 'osm_nominatim_identity')
        self.assertEqual(data['total_road_distance_miles'], 810.0)

    def test_cold_cache_one_lookup_per_chosen_stop_then_cached(self):
        resp, mock_client, resolver = self._setup_mocks(seed_cache=False)
        self.assertEqual(resp.status_code, 200)
        # First-time corridor: exactly one identity lookup for the one stop.
        self.assertEqual(mock_client.search_poi.call_count, 1)
        # ... and it was cached (a second resolve would not call again).
        self.assertIsNotNone(resolver.resolve(self._stations[0]))
        self.assertEqual(mock_client.search_poi.call_count, 1)


def _verified(lat, lon):
    return {'lat': lat, 'lon': lon, 'source': 'osm_nominatim_identity',
            'evidence': {'query': 'q', 'display_name': 'x', 'shared_tokens': ['KUM']}}


class FakeResolver:
    """Deterministic resolver stand-in: maps truckstop_id -> resolution/None."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def resolve(self, station, allow_junction=False):
        self.calls.append((station.truckstop_id, allow_junction))
        return self.mapping.get(station.truckstop_id)


def _road_result(legs):
    total = sum(legs)
    return RouteResult(start=RoutePoint(lat=0, lon=0), finish=RoutePoint(lat=0, lon=0),
                       distance_miles=total, distance_km=total * 1.609344,
                       duration_seconds=0, geometry=[], summary='waypoint-road-check',
                       legs_miles=legs)


class VerifiedPlanTests(TestCase):
    """plan_with_verification: identity-verified stops + road-leg math."""

    def _run_verified(self, route, stations, resolver, route_via_legs_list):
        from unittest.mock import Mock
        client = Mock()
        client.route_via.side_effect = [_road_result(l) for l in route_via_legs_list]
        opt = FuelOptimizer(mpg=10, max_range_miles=500, corridor_miles=10,
                            client=client, resolver=resolver)
        with patch('router.fuel_optimizer.loader') as mock_loader:
            mock_loader.located_stations.return_value = stations
            plan = opt.plan_with_verification(route, resolver)
        return plan, client

    def test_unverifiable_stop_excluded_and_replanned(self):
        route = make_route(900.0)
        cheap_bad = station_on_route(route, 400, 2.00, 'CHEAP BUT UNVERIFIABLE')
        import dataclasses as _dc
        cheap_bad = _dc.replace(cheap_bad, truckstop_id='bad')
        good = station_on_route(route, 450, 3.00, 'VERIFIABLE STOP')
        good = _dc.replace(good, truckstop_id='good')
        resolver = FakeResolver({'bad': None, 'good': _verified(40.0 + 450 / 69.0, -95.0)})
        plan, client = self._run_verified(route, [cheap_bad, good], resolver, [[450.0, 450.0]])
        self.assertEqual([s.station.truckstop_id for s in plan.stops], ['good'])
        s = plan.stops[0]
        self.assertEqual(s.location_precision, 'osm_nominatim_identity')
        self.assertEqual(s.position_source, 'osm_nominatim_identity')
        self.assertAlmostEqual(s.lat, 40.0 + 450 / 69.0, places=4)
        # Road math: 50-gal tank, 450 mi in -> 5 gal left, 450 mi home -> buy 40.
        self.assertAlmostEqual(s.gallons, 40.0, places=1)
        self.assertEqual(plan.total_road_distance_miles, 900.0)
        # route_via only called once the whole plan was verified.
        self.assertEqual(client.route_via.call_count, 1)

    def test_road_purchases_use_road_legs_not_straight_line(self):
        route = make_route(1200.0)
        s1 = station_on_route(route, 400, 3.00, 'STOP A'); s1 = _dc.replace(s1, truckstop_id='a')
        s2 = station_on_route(route, 850, 2.50, 'STOP B'); s2 = _dc.replace(s2, truckstop_id='b')
        resolver = FakeResolver({'a': _verified(s1.lat, s1.lon), 'b': _verified(s2.lat, s2.lon)})
        # Road legs are longer than the straight-line route miles.
        plan, client = self._run_verified(route, [s1, s2], resolver,
                                          [[430.0, 470.0, 380.0]])
        a, b = plan.stops
        # Arrival at A: 50 - 43 = 7 gal. B is cheaper and 470 road mi away
        # (within a full 500-mi tank): buy exactly enough to reach B on roads.
        self.assertAlmostEqual(a.gallons, 47.0 - 7.0, places=1)
        # B: remaining 380 road mi needs 38 gal; arrival 0 -> buy 38.
        self.assertAlmostEqual(b.gallons, 38.0, places=1)
        self.assertEqual(plan.total_road_distance_miles, 1280.0)
        self.assertAlmostEqual(plan.total_gallons_consumed, 128.0, places=1)
        # Road detours attributed: extra = (430-400) + (470-450) + (380-350)
        self.assertGreater(a.road_detour_miles, 0)
        self.assertEqual(a.detour_miles, a.road_detour_miles)

    def test_infeasible_road_leg_triggers_margin_replan(self):
        route = make_route(900.0)
        st = station_on_route(route, 400, 3.00, 'ONLY STOP'); st = _dc.replace(st, truckstop_id='x')
        resolver = FakeResolver({'x': _verified(st.lat, st.lon)})
        plan, client = self._run_verified(route, [st], resolver,
                                          [[410.0, 520.0],   # 520 > 500: infeasible
                                           [400.0, 490.0]])  # replanned with margin: feasible
        self.assertEqual(client.route_via.call_count, 2)
        self.assertEqual(len(plan.stops), 1)

    def test_margins_exhausted_means_explicit_infeasible(self):
        route = make_route(900.0)
        st = station_on_route(route, 400, 3.00, 'ONLY STOP'); st = _dc.replace(st, truckstop_id='x')
        resolver = FakeResolver({'x': _verified(st.lat, st.lon)})
        with self.assertRaises(NoFeasiblePlan):
            self._run_verified(route, [st], resolver,
                               [[410.0, 520.0], [410.0, 520.0], [410.0, 520.0]])

    def test_one_tank_trip_skips_verification_entirely(self):
        route = make_route(300.0)
        from unittest.mock import Mock
        client = Mock()
        resolver = FakeResolver({})
        opt = FuelOptimizer(mpg=10, max_range_miles=500, corridor_miles=10,
                            client=client, resolver=resolver)
        with patch('router.fuel_optimizer.loader') as mock_loader:
            mock_loader.located_stations.return_value = []
            plan = opt.plan_with_verification(route, resolver)
        self.assertEqual(plan.stops, [])
        client.route_via.assert_not_called()
        self.assertEqual(resolver.calls, [])


class ResolverIdentityTests(TestCase):
    """Identity binding: a nearby POI is NOT proof of this station."""

    def _resolver(self, candidates, seed=None):
        from unittest.mock import Mock
        from .resolver import StationResolver
        import tempfile, os
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'resolutions.json')
        if seed:
            import json as _json
            with open(path, 'w') as f:
                _json.dump(seed, f)
        client = Mock()
        client.search_poi.return_value = candidates
        r = StationResolver(client=client, cache_path=path)
        import time as _time
        r._last_call = _time.time()  # no rate-limit sleep in tests
        return r, client

    def _station(self, name='KUM & GO #0370'):
        return make_station(name=name, city='Gretna', state='NE',
                            lat=41.14, lon=-96.19)

    def _cand(self, lat, lon, name):
        return {'lat': lat, 'lon': lon, 'display_name': name,
                'osm_class': 'amenity', 'osm_type': 'fuel', 'osm_id': 1}

    def test_accepts_distinctive_token_match(self):
        r, client = self._resolver([
            self._cand(41.141, -96.191, 'Kum & Go, 1200 Frontage Rd, Gretna, Nebraska, USA')])
        res = r.resolve(self._station())
        self.assertIsNotNone(res)
        self.assertEqual(res['source'], 'osm_nominatim_identity')
        self.assertIn('KUM', res['evidence']['shared_tokens'])
        self.assertIn('OpenStreetMap', res['evidence']['attribution'])

    def test_rejects_generic_tokens_only(self):
        r, client = self._resolver([
            self._cand(41.141, -96.191, 'Travel Center, Gretna, Nebraska, USA')])
        self.assertIsNone(r.resolve(make_station(name='SOME TRAVEL CENTER',
                                                 city='Gretna', state='NE',
                                                 lat=41.14, lon=-96.19)))

    def test_rejects_two_same_chain_branches(self):
        # Two different Kum & Go branches in the same city: ambiguous.
        r, client = self._resolver([
            self._cand(41.141, -96.191, 'Kum & Go, North Exit, Gretna, Nebraska, USA'),
            self._cand(41.10, -96.15, 'Kum & Go, South Exit, Gretna, Nebraska, USA')])
        self.assertIsNone(r.resolve(self._station()))

    def test_rejects_out_of_radius(self):
        r, client = self._resolver([
            self._cand(41.45, -96.19, 'Kum & Go, Far Away, Nebraska, USA')])  # ~21 mi
        self.assertIsNone(r.resolve(self._station()))

    def test_cache_positive_and_negative_no_repeat_calls(self):
        r, client = self._resolver([
            self._cand(41.141, -96.191, 'Kum & Go, Gretna, Nebraska, USA')])
        st = self._station()
        self.assertIsNotNone(r.resolve(st))
        self.assertIsNotNone(r.resolve(st))
        self.assertEqual(client.search_poi.call_count, 1)

        r2, client2 = self._resolver([], seed=None)  # empty result -> negative
        st2 = make_station(name='NOMATCH STOP', city='Gretna', state='NE',
                           lat=41.14, lon=-96.19)
        st2 = _dc.replace(st2, truckstop_id='2')
        self.assertIsNone(r2.resolve(st2))
        self.assertIsNone(r2.resolve(st2))
        self.assertEqual(client2.search_poi.call_count, 1)

    def test_stale_cache_entry_is_refreshed(self):
        seed = {'1': {'ts': 0, 'resolved': True,
                      'result': {'lat': 1.0, 'lon': 1.0,
                                 'source': 'osm_nominatim_identity', 'evidence': {}}}}
        r, client = self._resolver(
            [self._cand(41.141, -96.191, 'Kum & Go, Gretna, Nebraska, USA')], seed=seed)
        st = self._station()
        st = _dc.replace(st, truckstop_id='1')
        res = r.resolve(st)
        self.assertEqual(client.search_poi.call_count, 1)   # re-fetched, not trusted
        self.assertNotEqual(res['lat'], 1.0)


class ResolverJunctionTierTests(TestCase):
    """Tier 2: offline exit-junction resolution bound to the CSV address."""

    INDEX = {
        'ne|I80|257': {'nodes': [[40.6902, -99.3802, 111], [40.6903, -99.3803, 112]]},
    }

    def _resolver(self, poi_candidates, index=None):
        from unittest.mock import Mock
        from .resolver import StationResolver
        import tempfile, os, json as _json
        tmp = tempfile.mkdtemp()
        ipath = os.path.join(tmp, 'junctions.json')
        with open(ipath, 'w') as f:
            _json.dump(self.INDEX if index is None else index, f)
        client = Mock()
        client.search_poi.return_value = poi_candidates
        r = StationResolver(client=client, cache_path=os.path.join(tmp, 'res.json'),
                            junction_index_path=ipath)
        import time as _time
        r._last_call = _time.time()
        return r, client

    def _station(self, address='I-80 & US-183, EXIT 257', name='ONE9 #901'):
        return FuelStation(truckstop_id='6093', name=name, address=address,
                           city='Elm Creek', state='NE', retail_price=2.7,
                           lat=40.7174, lon=-99.3733,
                           location_precision='city_centroid')

    def test_address_exit_resolves_when_poi_fails(self):
        r, client = self._resolver([])  # no POI candidates at all
        res = r.resolve(self._station(), allow_junction=True)
        self.assertIsNotNone(res)
        self.assertEqual(res['source'], 'csv_address_exit_junction')
        self.assertAlmostEqual(res['lat'], 40.69025, places=3)
        self.assertEqual(res['evidence']['osm_junction_nodes'], [111, 112])
        self.assertIn('not separately verified', res['evidence']['caveat'])

    def test_no_silent_highway_correction(self):
        # 'US-80, EXIT 257' must NOT be silently rewritten to I-80:
        # changing the highway in the address changes identity on a guess.
        r, _ = self._resolver([])
        self.assertIsNone(r.resolve(self._station(address='US-80, EXIT 257'),
                                    allow_junction=True))

    def test_strict_mode_never_uses_junction(self):
        # Junction vicinity is an explicitly approximate fallback only.
        r, _ = self._resolver([])
        self.assertIsNone(r.resolve(self._station()))  # strict default

    def test_no_exit_in_address_rejected(self):
        r, _ = self._resolver([])
        self.assertIsNone(r.resolve(self._station(address='100 Main St'),
                                    allow_junction=True))

    def test_spread_out_junction_rejected(self):
        idx = {'ne|I80|257': {'nodes': [[40.69, -99.38, 1], [40.0, -99.0, 2]]}}
        r, _ = self._resolver([], index=idx)
        self.assertIsNone(r.resolve(self._station(), allow_junction=True))

    def test_junction_out_of_radius_rejected(self):
        r, _ = self._resolver([])
        far = FuelStation(truckstop_id='9', name='X', address='I-80, EXIT 257',
                          city='Faraway', state='NE', retail_price=2.7,
                          lat=42.5, lon=-99.38, location_precision='city_centroid')
        self.assertIsNone(r.resolve(far, allow_junction=True))

    def test_negative_caches_reason(self):
        import json as _json
        r, client = self._resolver([])
        self.assertIsNone(r.resolve(self._station(address='100 Main St'),
                                    allow_junction=True))
        self.assertIsNone(r.resolve(self._station(address='100 Main St'),
                                    allow_junction=True))
        self.assertEqual(client.search_poi.call_count, 1)  # cached, no repeat
        data = _json.load(open(r.cache_path))
        self.assertIn('junction:no_exit_in_address', data['6093']['reason'])


class LookupBudgetTests(TestCase):
    """max_attempts bounds loop iterations, NOT live lookups: the global
    max_station_lookups cap is the real per-request bound."""

    def test_global_lookup_cap_trips_with_clear_error(self):
        route = make_route(890.0)
        import dataclasses as _dc
        stations = []
        mapping = {}
        for i in range(6):
            s = station_on_route(route, 400 + i * 10, 2.00 + i * 0.01, f'STOP {i}')
            s = _dc.replace(s, truckstop_id=f's{i}')
            stations.append(s)
            mapping[f's{i}'] = None   # every candidate unverifiable

        class CountingResolver(FakeResolver):
            def __init__(self, m):
                super().__init__(m)
                self.calls_made = 0

            def resolve(self, station, allow_junction=False):
                self.calls_made += 1
                return None

        from unittest.mock import Mock
        resolver = CountingResolver(mapping)
        opt = FuelOptimizer(mpg=10, max_range_miles=500, corridor_miles=10,
                            client=Mock(), resolver=resolver)
        with patch('router.fuel_optimizer.loader') as mock_loader:
            mock_loader.located_stations.return_value = stations
            with self.assertRaises(NoFeasiblePlan) as ctx:
                opt.plan_with_verification(route, resolver, max_station_lookups=3)
        self.assertIn('lookup budget', str(ctx.exception))
        self.assertLessEqual(resolver.calls_made, 4)  # capped, not 20 attempts x N stops
