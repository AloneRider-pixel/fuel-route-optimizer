# Generated for the along-route station fix on 2026-11-17

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('router', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='fuelstop',
            name='total_gallons_consumed',
            field=models.FloatField(default=0, help_text='Total gallons the trip consumes (distance / mpg), including the starting tank.'),
        ),
        migrations.AddField(
            model_name='fuelstopdetail',
            name='detour_miles',
            field=models.FloatField(blank=True, help_text="Straight-line distance of the station's city from the route (miles).", null=True),
        ),
        migrations.AddField(
            model_name='fuelstopdetail',
            name='location_precision',
            field=models.CharField(blank=True, default='', help_text='Precision of the stored coordinates, e.g. city_centroid.', max_length=32),
        ),
    ]
