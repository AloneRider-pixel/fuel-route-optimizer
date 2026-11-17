"""
Generate a standalone HTML map of the route with fuel-stop markers.

Uses folium (OpenStreetMap tiles). The map is saved under MEDIA_ROOT and served
via /media/, so the API can return a URL the client can open in a browser.
"""

import folium
from django.conf import settings
from pathlib import Path

from .routing import RoutePoint


def build_route_map(route_geometry, stops, start_point: RoutePoint,
                    finish_point: RoutePoint, output_name: str = 'route_map.html') -> str:
    """
    Draw a polyline for the route and markers for each fuel stop.

    Returns the absolute filesystem path of the saved HTML file.
    """
    media_root = getattr(settings, 'MEDIA_ROOT', None)
    if media_root is None:
        raise RuntimeError("settings.MEDIA_ROOT is not configured")
    media_root = Path(media_root)
    media_root.mkdir(parents=True, exist_ok=True)

    # Center map on route start.
    center = [start_point.lat, start_point.lon]
    m = folium.Map(location=center, zoom_start=5, tiles='OpenStreetMap')

    # Route polyline (lat, lon) pairs.
    line = [[p.lat, p.lon] for p in route_geometry]
    folium.PolyLine(
        locations=line,
        color='#0066cc',
        weight=5,
        opacity=0.8,
        tooltip='Route',
    ).add_to(m)

    # Start / finish markers.
    folium.Marker(
        [start_point.lat, start_point.lon],
        popup=f"<b>Start</b><br>{start_point.lat:.4f}, {start_point.lon:.4f}",
        icon=folium.Icon(color='green', icon='play'),
    ).add_to(m)
    folium.Marker(
        [finish_point.lat, finish_point.lon],
        popup=f"<b>Finish</b><br>{finish_point.lat:.4f}, {finish_point.lon:.4f}",
        icon=folium.Icon(color='red', icon='stop'),
    ).add_to(m)

    # Fuel stop markers: at each stop's reported position - an identity-
    # verified station position (strict) or a labelled exit-junction
    # vicinity point (approximate); position_source labels which.
    for s in stops:
        if s.lat is None or s.lon is None:
            continue
        name = s.station.name if s.station else f"Stop {s.order}"
        city = s.station.city if s.station else ''
        state = s.station.state if s.station else ''
        price = f"${s.station.retail_price:.3f}" if s.station else "n/a"
        precision = s.location_precision or 'approximate'
        detour = f"{s.detour_miles} mi off route" if s.detour_miles is not None else "on route"
        popup = (
            f"<b>Fuel Stop {s.order}</b><br>"
            f"{name}<br>{city}, {state}<br>"
            f"Price: {price}/gal<br>"
            f"Route mile: {s.mile_marker}<br>"
            f"Gallons: {s.gallons}<br>Cost: ${s.cost_usd:.2f}<br>"
            f"<i>Marker: {precision.replace('_', ' ')} ({detour})</i>"
        )
        folium.Marker(
            [s.lat, s.lon],
            popup=popup,
            tooltip=f"Stop {s.order} - {name}",
            icon=folium.Icon(color='orange', icon='tint', prefix='fa'),
        ).add_to(m)

    out_path = media_root / output_name
    m.save(str(out_path))
    return str(out_path)
