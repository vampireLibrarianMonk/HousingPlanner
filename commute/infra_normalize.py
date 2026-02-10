from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Iterable, List


def _parse_date(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        try:
            dt = datetime.utcfromtimestamp(value / 1000.0)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
        return value
    return None


def normalize_waze_alerts(alerts: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        location = alert.get("location") or {}
        lat = location.get("y") or alert.get("latitude")
        lon = location.get("x") or alert.get("longitude")
        geom = {
            "type": "Point",
            "coordinates": [lon, lat],
        }
        normalized.append(
            {
                "event_id": str(alert.get("uuid") or alert.get("id") or "waze"),
                "event_type": "incident",
                "source": "waze",
                "geometry_type": "point",
                "severity": "low",
                "status": "active",
                "start_date": _parse_date(alert.get("pubMillis") or alert.get("publish_datetime_utc")),
                "end_date": None,
                "confidence": "medium",
                "description": (
                    alert.get("description")
                    or alert.get("type")
                    or alert.get("subtype")
                    or "Waze alert"
                ),
                "geometry": geom,
            }
        )
    return normalized


def normalize_waze_jams(jams: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized = []
    for jam in jams:
        if not isinstance(jam, dict):
            continue
        coords = jam.get("line_coordinates") or []
        line_coords = []
        for coord in coords:
            lat = coord.get("lat")
            lon = coord.get("lon")
            if lat is None or lon is None:
                continue
            line_coords.append([lon, lat])
        if not line_coords:
            continue
        normalized.append(
            {
                "event_id": str(jam.get("jam_id") or "waze-jam"),
                "event_type": "jam",
                "source": "waze",
                "geometry_type": "line",
                "severity": "medium" if jam.get("severity", 0) >= 4 else "low",
                "status": "active",
                "start_date": _parse_date(jam.get("publish_datetime_utc")),
                "end_date": None,
                "confidence": "medium",
                "description": (
                    jam.get("block_alert_description")
                    or jam.get("street")
                    or "Traffic jam"
                ),
                "geometry": {
                    "type": "LineString",
                    "coordinates": line_coords,
                },
                "metadata": {
                    "speed_kmh": jam.get("speed_kmh"),
                    "delay_seconds": jam.get("delay_seconds"),
                    "length_meters": jam.get("length_meters"),
                    "severity": jam.get("severity"),
                },
            }
        )
    return normalized