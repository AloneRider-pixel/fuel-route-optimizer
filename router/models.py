"""
Models for the fuel-route router app.

The core fuel-stop optimization is computed on-the-fly from the CSV data and
the OSRM route geometry. A lightweight FuelStop record is persisted per route
request so the computed (cost-effective) stops can be inspected later.
"""

from django.db import models
from django.utils import timezone


class FuelStop(models.Model):
    """
    One computed route plan (origin, destination, totals) - the parent
    record; its individual stops live in FuelStopDetail. (Historical name:
    the model predates the per-stop detail table.)
    """

    class Meta:
        ordering = ['-created_at']

    start_location = models.CharField(max_length=255, help_text="Origin of the route.")
    finish_location = models.CharField(max_length=255, help_text="Destination of the route.")

    total_distance_miles = models.FloatField(help_text="Total route distance in miles.")
    total_fuel_cost_usd = models.FloatField(help_text="Total money spent on fuel (USD).")
    total_gallons = models.FloatField(help_text="Total gallons purchased en route.")
    total_gallons_consumed = models.FloatField(
        default=0, help_text="Total gallons the trip consumes (distance / mpg), including the starting tank.")

    mpg = models.FloatField(default=10, help_text="Vehicle miles per gallon.")
    max_range_miles = models.FloatField(default=500, help_text="Max vehicle range on a full tank (miles).")
    total_road_distance_miles = models.FloatField(
        null=True, blank=True,
        help_text="Actual road distance of the finished plan incl. stop detours (verified plans).")
    verification_mode = models.CharField(
        max_length=20, default='strict',
        help_text="'strict': all stops identity-verified actual station positions; "
                  "'approximate': some stops are labelled exit-junction vicinity positions.")

    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"Route {self.pk}: {self.start_location} -> {self.finish_location}"


class FuelStopDetail(models.Model):
    """
    An individual stop within a FuelStop route: where to fuel, price, gallons, cost.
    """

    route = models.ForeignKey(FuelStop, on_delete=models.CASCADE, related_name='stops')

    order = models.PositiveIntegerField(help_text="Visit order along the route (1 = first stop).")
    mile_marker = models.FloatField(help_text="Distance from origin (miles) where this stop occurs.")

    truckstop_id = models.CharField(max_length=64, blank=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255, blank=True)
    city = models.CharField(max_length=100, blank=True)
    state = models.CharField(max_length=10, blank=True)
    retail_price = models.FloatField(help_text="Fuel price per gallon at this stop.")

    gallons = models.FloatField(help_text="Gallons purchased at this stop.")
    cost_usd = models.FloatField(help_text="Cost for this stop (gallons * price).")

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    detour_miles = models.FloatField(
        null=True, blank=True,
        help_text="Extra miles off the route: road-derived for verified stops, straight-line city-level otherwise.")
    location_precision = models.CharField(
        max_length=32, blank=True, default='',
        help_text="Precision/source of the stored coordinates, e.g. city_centroid or osm_nominatim_identity.")
    position_source = models.CharField(
        max_length=40, blank=True, default='',
        help_text="How the stored position was established (empty = city centroid fallback).")
    road_detour_miles = models.FloatField(
        null=True, blank=True,
        help_text="Road-derived extra miles for this stop (verified plans only).")

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Stop {self.order}: {self.name} ({self.city}, {self.state}) @ ${self.retail_price}/gal"