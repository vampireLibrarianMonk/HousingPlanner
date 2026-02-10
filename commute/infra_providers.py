from __future__ import annotations

from typing import Any, Dict, Optional

import requests

from config.urls import WAZE_ALERTS_JAMS_URL


def fetch_waze_alerts(
    api_key: str,
    bbox: tuple[float, float, float, float],
    timeout: int = 20,
    max_alerts: int = 50,
    max_jams: int = 50,
) -> Dict[str, Any]:
    min_lon, min_lat, max_lon, max_lat = bbox
    params = {
        "bottom_left": f"{min_lat},{min_lon}",
        "top_right": f"{max_lat},{max_lon}",
        "max_alerts": max_alerts,
        "max_jams": max_jams,
    }
    headers = {"X-API-Key": api_key}
    resp = requests.get(WAZE_ALERTS_JAMS_URL, params=params, headers=headers, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_waze_incidents(
    api_key: Optional[str],
    bbox: tuple[float, float, float, float],
) -> Dict[str, Any]:
    if not api_key:
        return {"alerts": [], "jams": []}
    return fetch_waze_alerts(api_key=api_key, bbox=bbox)