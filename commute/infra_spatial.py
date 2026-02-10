from __future__ import annotations

from typing import Iterable, List, Dict, Any

from shapely.geometry import LineString, Point, shape


def build_route_buffer(points: Iterable[tuple[float, float]], meters: float) -> LineString:
    line = LineString([(lon, lat) for lat, lon in points])
    # crude conversion: 1 deg ~ 111km
    buffer_deg = meters / 111_000.0
    return line.buffer(buffer_deg)


def filter_events_by_buffer(events: Iterable[Dict[str, Any]], buffer_geom) -> List[Dict[str, Any]]:
    filtered = []
    for event in events:
        if not isinstance(event, dict):
            continue
        geom = event.get("geometry") or {}
        try:
            event_geom = shape(geom)
        except Exception:
            coords = geom.get("coordinates")
            if coords:
                event_geom = Point(coords)
            else:
                continue
        if buffer_geom.intersects(event_geom):
            filtered.append(event)
    return filtered