from django.contrib import admin
from .models import FuelStop, FuelStopDetail


class FuelStopDetailInline(admin.TabularInline):
    model = FuelStopDetail
    extra = 0
    readonly_fields = ('order', 'mile_marker', 'name', 'city', 'state',
                       'retail_price', 'gallons', 'cost_usd')


@admin.register(FuelStop)
class FuelStopAdmin(admin.ModelAdmin):
    list_display = ('id', 'start_location', 'finish_location',
                    'total_distance_miles', 'total_fuel_cost_usd',
                    'total_gallons', 'created_at')
    list_filter = ('created_at',)
    search_fields = ('start_location', 'finish_location')
    inlines = [FuelStopDetailInline]
