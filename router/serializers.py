"""
DRF serializers for the fuel-route API.
"""

from rest_framework import serializers

from .models import FuelStop, FuelStopDetail


class FuelStopDetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = FuelStopDetail
        fields = [
            'order',
            'mile_marker',
            'truckstop_id',
            'name',
            'address',
            'city',
            'state',
            'retail_price',
            'gallons',
            'cost_usd',
            'latitude',
            'longitude',
            'detour_miles',
            'location_precision',
            'position_source',
            'road_detour_miles',
        ]


class FuelStopSerializer(serializers.ModelSerializer):
    stops = FuelStopDetailSerializer(many=True, read_only=True)

    class Meta:
        model = FuelStop
        fields = [
            'id',
            'start_location',
            'finish_location',
            'total_distance_miles',
            'total_fuel_cost_usd',
            'total_gallons',
            'total_gallons_consumed',
            'total_road_distance_miles',
            'verification_mode',
            'mpg',
            'max_range_miles',
            'created_at',
            'stops',
        ]


class RouteRequestSerializer(serializers.Serializer):
    """
    Input validation for the route/fuel-optimization endpoint.

    Accepts either plain addresses/city-states (USA) or lat,lng coordinates.
    """

    start = serializers.CharField(
        max_length=255,
        help_text="Start location: address/city,state (USA) or 'lat,lng'.",
    )
    finish = serializers.CharField(
        max_length=255,
        help_text="Finish location: address/city,state (USA) or 'lat,lng'.",
    )
    mode = serializers.ChoiceField(
        choices=['strict', 'approximate'], default='strict', required=False,
        help_text="'strict' (default): only identity-verified actual station "
                  "positions; fails clearly when no verified chain is reachable. "
                  "'approximate': explicit opt-in that also permits labelled "
                  "exit-junction vicinity positions for unresolved stations.",
    )

    def validate_start(self, value):
        return _clean_location(value)

    def validate_finish(self, value):
        return _clean_location(value)


def _clean_location(value: str) -> str:
    value = value.strip()
    if not value:
        raise serializers.ValidationError("Location cannot be empty.")
    return value