"""
Heat Risk integration using NOAA/NWS HeatRisk ImageServer.

Provides:
- Point-based screening summary (identify)
- Raster overlay for the exact search radius (exportImage)
"""

from __future__ import annotations

import requests
import streamlit as st
import pyproj

HEATRISK_IMAGE_SERVER = (
    "https://mapservices.weather.noaa.gov/experimental/rest/services/"
    "NWS_HeatRisk/ImageServer"
)

HEATRISK_LABELS = {
    0: "None",
    1: "Minor",
    2: "Moderate",
    3: "Major",
    4: "Extreme",
}

HEATRISK_COLORS = {
    0: "#E0F7FA",
    1: "#FFF59D",
    2: "#FFD54F",
    3: "#FF8A65",
    4: "#D32F2F",
}

NWS_ALERTS_API = "https://api.weather.gov/alerts"
NWS_HEAT_EVENT_TYPES = [
    "Heat Advisory",
    "Excessive Heat Warning",
    "Excessive Heat Watch",
]

IEM_VTEC_BYPOINT_API = "https://mesonet.agron.iastate.edu/json/vtec_events_bypoint.py"
IEM_VTEC_EVENT_GEOJSON_API = "https://mesonet.agron.iastate.edu/geojson/vtec_event.py"
IEM_HEAT_PHENOMENA = {"HT", "EH", "XH"}


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_heatrisk_latest_time() -> int | None:
    r = requests.get(f"{HEATRISK_IMAGE_SERVER}?f=pjson", timeout=30)
    r.raise_for_status()
    data = r.json()
    time_info = data.get("timeInfo") or {}
    time_extent = time_info.get("timeExtent")
    if not time_extent:
        return None
    return time_extent[1]


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_heatrisk_point(lat: float, lon: float, time_ms: int | None = None) -> dict | None:
    if time_ms is None:
        time_ms = fetch_heatrisk_latest_time()
    transformer = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    x, y = transformer.transform(lon, lat)
    params = {
        "f": "json",
        "geometry": f"{x},{y}",
        "geometryType": "esriGeometryPoint",
        "sr": 3857,
    }

    if time_ms is not None:
        params["time"] = time_ms

    r = requests.get(f"{HEATRISK_IMAGE_SERVER}/identify", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    if data.get("value") in (None, "NoData"):
        params["geometry"] = f"{lon},{lat}"
        params["sr"] = 4326
        r = requests.get(f"{HEATRISK_IMAGE_SERVER}/identify", params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

    value = data.get("value")
    if value is None or str(value).lower() == "nodata":
        return None

    try:
        value_int = int(value)
    except (TypeError, ValueError):
        value_int = None

    return {
        "value": value_int,
        "label": HEATRISK_LABELS.get(value_int, "Unknown"),
    }


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_heatrisk_raster(bbox, time_ms: int | None = None) -> dict | None:
    if time_ms is None:
        time_ms = fetch_heatrisk_latest_time()
    west, south, east, north = bbox
    params = {
        "f": "json",
        "bbox": f"{west},{south},{east},{north}",
        "bboxSR": 4326,
        "imageSR": 4326,
        "format": "png",
        "transparent": "true",
        "size": "800,800",
    }

    if time_ms is not None:
        params["time"] = time_ms

    r = requests.get(f"{HEATRISK_IMAGE_SERVER}/exportImage", params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    if "href" not in data:
        return None

    return {
        "href": data["href"],
        "extent": data.get("extent"),
    }


def heatrisk_legend_items():
    return [
        ("None", HEATRISK_COLORS[0]),
        ("Minor", HEATRISK_COLORS[1]),
        ("Moderate", HEATRISK_COLORS[2]),
        ("Major", HEATRISK_COLORS[3]),
        ("Extreme", HEATRISK_COLORS[4]),
    ]


def _alert_matches_heat(event_name: str | None) -> bool:
    if not event_name:
        return False
    return event_name in NWS_HEAT_EVENT_TYPES


@st.cache_data(show_spinner=False, ttl=1800)
def fetch_nws_heat_alerts(lat: float, lon: float, days: int = 30) -> dict:
    """
    Fetch active + recent heat alerts within the last N days near a point.
    """
    params = {
        "point": f"{lat},{lon}",
    }

    r = requests.get(NWS_ALERTS_API, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    active = []
    recent = []

    for feature in data.get("features", []) or []:
        props = feature.get("properties") or {}
        event = props.get("event")
        if not _alert_matches_heat(event):
            continue

        item = {
            "event": event,
            "headline": props.get("headline"),
            "severity": props.get("severity"),
            "urgency": props.get("urgency"),
            "certainty": props.get("certainty"),
            "effective": props.get("effective"),
            "expires": props.get("expires"),
            "onset": props.get("onset"),
            "ends": props.get("ends"),
        }

        if props.get("status") == "Actual" and props.get("messageType") in {"Alert", "Update"}:
            if props.get("severity") in {"Severe", "Extreme", "Moderate"}:
                active.append(item)
            else:
                recent.append(item)
        else:
            recent.append(item)

    return {
        "active": active,
        "recent": recent,
    }


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_historical_heat_events(lat: float, lon: float, days: int = 30) -> list[dict]:
    """
    Fetch historical heat advisory/warning events for the past N days using IEM VTEC.
    """
    from datetime import datetime, timedelta, timezone

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    params = {
        "lat": lat,
        "lon": lon,
        "sdate": start.strftime("%Y-%m-%d"),
        "edate": end.strftime("%Y-%m-%d"),
        "fmt": "json",
    }

    r = requests.get(IEM_VTEC_BYPOINT_API, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    events = []
    for item in data.get("events", []) or []:
        phenomena = item.get("phenomena")
        significance = item.get("significance")
        if phenomena not in IEM_HEAT_PHENOMENA:
            continue

        events.append({
            "name": item.get("name"),
            "issue": item.get("issue"),
            "expire": item.get("expire"),
            "phenomena": phenomena,
            "significance": significance,
            "wfo": item.get("wfo"),
            "ugc": item.get("ugc"),
            "eventid": item.get("eventid"),
        })

    return events


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_heat_event_geojson(event: dict) -> dict | None:
    """Fetch GeoJSON polygon for a single VTEC event."""
    if not event:
        return None

    params = {
        "year": int(event.get("issue", "")[:4] or 0),
        "wfo": event.get("wfo"),
        "phenomena": event.get("phenomena"),
        "significance": event.get("significance"),
        "etn": event.get("eventid") or event.get("etn"),
    }

    if not params["year"] or not params["wfo"] or not params["etn"]:
        return None

    r = requests.get(IEM_VTEC_EVENT_GEOJSON_API, params=params, timeout=20)
    r.raise_for_status()
    return r.json()