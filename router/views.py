"""
Views for the fuel-route API.

POST /api/route/
  Body: { "start": "Chicago, IL", "finish": "Denver, CO" }
  (or "41.8781,-87.6298" style coordinates)

Returns JSON with the route, cost-effective fuel stops ALONG the route,
totals, and a map URL. Chosen stops are identity-verified against
OpenStreetMap (one cached, user-triggered lookup per station) and the
finished plan is measured on actual roads with one extra OSRM waypoint
call. Station candidate pre-filtering uses local preprocessed data, so
picking candidates costs no API calls. The map reuses the optimizer's
route geometry.
"""

import uuid

from django.conf import settings

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import FuelStop, FuelStopDetail
from .serializers import RouteRequestSerializer, FuelStopSerializer
from .fuel_optimizer import FuelOptimizer, NoFeasiblePlan
from .resolver import StationResolver
from .map_gen import build_route_map


class RouteOptimizeView(APIView):
    """
    Compute an optimized, cost-effective fuel plan between two USA locations.
    """

    def post(self, request):
        serializer = RouteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        start = serializer.validated_data['start']
        finish = serializer.validated_data['finish']
        mode = (serializer.validated_data.get('mode') or 'strict').lower()
        if mode not in ('strict', 'approximate'):
            return Response(
                {'error': 'invalid_mode',
                 'detail': "mode must be 'strict' (default; only identity-verified "
                           "actual station positions) or 'approximate' (explicit "
                           "opt-in permitting labelled exit-junction vicinity "
                           "positions for unresolved stations)."},
                status=status.HTTP_400_BAD_REQUEST)

        # The resolver shares the optimizer's HTTP client so every external
        # call carries the same identifying User-Agent.
        from .routing import OSRMClient
        client = OSRMClient()
        optimizer = FuelOptimizer(
            mpg=float(getattr(settings, 'VEHICLE_MPG', 10)),
            max_range_miles=float(getattr(settings, 'VEHICLE_RANGE_MILES', 500)),
            client=client,
            resolver=StationResolver(client=client),
        )

        try:
            plan = optimizer.plan(start, finish, mode=mode)
        except NoFeasiblePlan as exc:
            # Honest failure: no reachable station completes the trip.
            return Response(
                {'error': 'no_feasible_plan', 'detail': str(exc)},
                status=status.HTTP_422_UNPROCESSABLE_ENTITY,
            )
        except Exception as exc:  # network / routing failures
            return Response(
                {'error': 'Routing failed', 'detail': str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        # Persist the plan so it can be inspected later.
        fuel_stop = FuelStop.objects.create(
            start_location=start,
            finish_location=finish,
            total_distance_miles=plan.total_distance_miles,
            total_fuel_cost_usd=plan.total_cost_usd,
            total_gallons=plan.total_gallons,
            total_gallons_consumed=plan.total_gallons_consumed,
            mpg=plan.mpg,
            max_range_miles=plan.max_range_miles,
            total_road_distance_miles=plan.total_road_distance_miles,
            verification_mode=plan.verification_mode,
        )
        for s in plan.stops:
            FuelStopDetail.objects.create(
                route=fuel_stop,
                order=s.order,
                mile_marker=s.mile_marker,
                truckstop_id=s.station.truckstop_id if s.station else '',
                name=s.station.name if s.station else f'Stop {s.order}',
                address=s.station.address if s.station else '',
                city=s.station.city if s.station else '',
                state=s.station.state if s.station else '',
                retail_price=s.station.retail_price if s.station else 0.0,
                gallons=s.gallons,
                cost_usd=s.cost_usd,
                latitude=s.lat,
                longitude=s.lon,
                detour_miles=s.detour_miles,
                location_precision=s.location_precision or '',
                position_source=s.position_source or '',
                road_detour_miles=s.road_detour_miles,
            )

        # Build the map from the SAME route geometry the optimizer used -
        # no additional geocoding or routing calls.
        map_url = None
        if plan.route is not None:
            try:
                map_name = f'route_{fuel_stop.id}_{uuid.uuid4().hex[:8]}.html'
                build_route_map(
                    plan.route.geometry,
                    plan.stops,
                    plan.route.start,
                    plan.route.finish,
                    output_name=map_name,
                )
                media_url = getattr(settings, 'MEDIA_URL', '/media/')
                map_url = media_url.rstrip('/') + '/' + map_name
            except Exception:
                # Map is a nice-to-have; don't fail the whole request.
                map_url = None

        out = FuelStopSerializer(fuel_stop).data
        out['map_url'] = map_url
        verified = bool(plan.stops) and plan.total_road_distance_miles is not None
        approx = verified and any(
            s.position_source == 'csv_address_exit_junction' for s in plan.stops)
        out['verification_mode'] = plan.verification_mode if verified else 'not_required'
        out['note'] = (
            "Assumes a full tank at the start; tank capacity is the implied "
            "range/mpg (50 gal). total_fuel_cost_usd is money spent at stops "
            "en route, priced where bought. "
            + (
                "Stop positions are identity-verified against OpenStreetMap "
                "(position_source per stop; data (c) OSM contributors, ODbL), "
                "detour_miles is road-derived from an OSRM waypoint route, "
                "and purchases/range were computed on the actual road legs "
                f"(total_road_distance_miles={plan.total_road_distance_miles}; "
                "total_gallons_consumed = road distance / mpg). "
                "OSRM routes are car-profile, not a truck-access guarantee."
                if verified and not approx else
                "APPROXIMATE MODE: one or more stops use the highway exit "
                "named in the station's own CSV address "
                "(position_source='csv_address_exit_junction') - a vicinity "
                "position, NOT the verified station lot/entrance; the "
                "~0.5 mi access offset is unverified. "
                f"Road legs (total_road_distance_miles={plan.total_road_distance_miles}) "
                "and detours are measured to the junction. Data (c) OSM "
                "contributors, ODbL; OSRM routes are car-profile."
                if verified else
                "One-tank trip: no stops needed, no verification calls made. "
                "total_gallons_consumed is route distance/mpg drawn from the "
                "starting tank."
            )
        )
        return Response(out, status=status.HTTP_200_OK)


class RouteDetailView(APIView):
    """GET /api/route/<id>/ — retrieve a previously computed plan."""

    def get(self, request, pk):
        try:
            fuel_stop = FuelStop.objects.get(pk=pk)
        except FuelStop.DoesNotExist:
            return Response({'error': 'Not found'}, status=status.HTTP_404_NOT_FOUND)
        out = FuelStopSerializer(fuel_stop).data
        return Response(out)


class FuelPriceView(APIView):
    """GET /api/fuel-price/?state=TX — cheapest fuel price in a state."""

    def get(self, request):
        state = request.query_params.get('state')
        if not state:
            return Response({'error': 'state query param required'},
                            status=status.HTTP_400_BAD_REQUEST)
        from .fuel_data import loader
        loader.load()
        station = loader.cheapest_in_state(state)
        if not station:
            return Response({'error': f'No stations for state {state}'},
                            status=status.HTTP_404_NOT_FOUND)
        return Response({
            'state': station.state,
            'station': station.name,
            'city': station.city,
            'address': station.address,
            'retail_price': station.retail_price,
        })
