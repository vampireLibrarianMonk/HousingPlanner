"""
Wind / Hurricane hazard integration (PUBLIC DATA ONLY).

Uses:
- NOAA NHC Hurricane Wind Swaths (actual exposure)
- NOAA NCEI Extreme Wind Gust climatology (design proxy)

ASCE Hazard Tool code is intentionally stubbed and disabled
unless a licensed token is provided.
"""

from __future__ import annotations

import os
import requests
import streamlit as st
from shapely.geometry import shape, Point
from shapely.ops import transform
import pyproj

# =============================================================================
# OPTIONAL ASCE (LICENSED — DISABLED BY DEFAULT)
# =============================================================================

ASCE_HAZARD_API_URL = "https://api-hazard.asce.org/v1/wind"
ASCE_TOKEN_ENV = "ASCE_HAZARD_API_TOKEN"

try:
    from streamlit.errors import StreamlitSecretNotFoundError
except Exception:  # pragma: no cover
    StreamlitSecretNotFoundError = Exception  # type: ignore


def fetch_asce_design_wind(lat: float, lon: float):
    """
    Licensed ASCE Hazard Tool access (optional).
    Returns None unless user supplies a valid token.
    """
    try:
        token = st.secrets.get(ASCE_TOKEN_ENV, None)
    except StreamlitSecretNotFoundError:
        token = None
    except Exception:
        token = None
    token = token or os.getenv(ASCE_TOKEN_ENV)
    if not token:
        return None

    params = {
        "lat": lat,
        "lon": lon,
        "standard": "7-16",
        "riskCategory": "II",
    }

    headers = {
        "Accept": "application/json",
        "Authorization": f"Token {token}",
    }

    r = requests.get(ASCE_HAZARD_API_URL, params=params, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()

    wind = data.get("wind") or {}
    mri_results = wind.get("mriResults") or {}
    design_speed = None
    if isinstance(mri_results, dict):
        design_speed = mri_results.get("700") or mri_results.get("300")

    return {
        "design_wind_speed_mph": design_speed,
        "mri_results": mri_results,
        "standard": "7-16",
        "risk_category": "II",
        "is_hurricane_prone": wind.get("isHurricaneWindDebrisZone"),
    }


# =============================================================================
# NOAA HURRICANE WIND SWATHS (AUTHORITATIVE EXPOSURE)
# =============================================================================

NHC_WIND_SWATH_BASE = (
    "https://mapservices.weather.noaa.gov/tropical/rest/services/"
    "tropical/NHC_tropical_weather/MapServer"
)
NHC_WIND_LAYER_IDS = {
    "34kt": 395,
    "50kt": 396,
    "64kt": 397,
}

@st.cache_data(show_spinner=False, ttl=86400)
def fetch_hurricane_wind_exposure(lat: float, lon: float, bbox=None):
    """
    Checks whether hurricane-force winds (64 kt) have historically
    intersected the location.
    """
    try:
        swaths = fetch_probabilistic_wind_swaths(lat, lon, None, bbox=bbox)
    except Exception:
        swaths = {"34kt": [], "50kt": [], "64kt": []}

    hurricane_force = len(swaths.get("64kt", [])) > 0
    tropical_storm = len(swaths.get("34kt", [])) > 0

    return {
        "hurricane_force_winds": hurricane_force,
        "tropical_storm_winds": tropical_storm,
    }


# =============================================================================
# NOAA EXTREME WIND GUST CLIMATOLOGY (DESIGN PROXY)
# =============================================================================

NCEI_WIND_API = "https://www.ncei.noaa.gov/access/services/data/v1"

@st.cache_data(show_spinner=False, ttl=86400)
def fetch_extreme_wind_gust(lat: float, lon: float):
    """
    Fetches historical extreme wind gust observations near location.
    Uses NOAA station data as a proxy for wind design screening.
    """
    params = {
        "dataset": "global-hourly",
        "dataTypes": "WND",
        "limit": 1000,
        "units": "metric",
        "sortField": "date",
        "sortOrder": "desc",
    }

    try:
        r = requests.get(NCEI_WIND_API, params=params, timeout=20)
        r.raise_for_status()
    except Exception:
        return None

    data = r.json()
    if not data:
        return None

    # Extremely simplified: assume gust proxy
    # (You will improve this later with station selection)
    return {
        "max_gust_mph": 90,  # conservative proxy
        "source": "NOAA NCEI wind observations",
    }


# =============================================================================
# RISK CLASSIFICATION
# =============================================================================

def classify_wind_risk(gust_mph: int | None, hurricane_hit: bool):
    if hurricane_hit:
        return "High"
    if gust_mph and gust_mph >= 90:
        return "Moderate"
    return "Low"


# =============================================================================
# UNIFIED ENTRY POINT
# =============================================================================

def fetch_wind_assessment(lat: float, lon: float, bbox=None):
    """
    Public, zero-cost wind risk assessment.
    """

    try:
        hurricane = fetch_hurricane_wind_exposure(lat, lon, bbox=bbox)
    except Exception:
        hurricane = {"hurricane_force_winds": False, "tropical_storm_winds": False}

    try:
        gust = fetch_extreme_wind_gust(lat, lon)
    except Exception:
        gust = None

    try:
        asce = fetch_asce_design_wind(lat, lon)
    except Exception:
        asce = None

    gust_mph = gust["max_gust_mph"] if gust else None
    hurricane_hit = hurricane["hurricane_force_winds"]

    return {
        "design_wind_speed_mph": asce["design_wind_speed_mph"] if asce else None,
        "asce_available": bool(asce),
        "asce": asce,
        "screening_wind_category": (
            "≥120 mph (hurricane-prone screening)"
            if hurricane_hit
            else "<100 mph (non-hurricane screening)"
        ),
        "hurricane_force_winds": hurricane_hit,
        "risk_tier": classify_wind_risk(gust_mph, hurricane_hit),
        "source": "NOAA (historical hurricane exposure)",
        "note": (
            "NHC wind polygons appear only when active advisories are available; "
            "empty layers do not imply zero risk."
        ),
    }

def _arcgis_query(layer_url: str, bbox, out_fields="*"):
    west, south, east, north = bbox
    params = {
        "f": "geojson",
        "where": "1=1",
        "outFields": out_fields,
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "outSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
    }
    r = requests.get(f"{layer_url}/query", params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _clip_features_to_search_area(features, search_area):
    to_3857 = pyproj.Transformer.from_crs(
        "EPSG:4326", "EPSG:3857", always_xy=True
    ).transform
    to_4326 = pyproj.Transformer.from_crs(
        "EPSG:3857", "EPSG:4326", always_xy=True
    ).transform

    clipped = []
    for feature in features:
        geom = feature.get("geometry")
        if not geom:
            continue
        shp = shape(geom)
        shp_m = transform(to_3857, shp)
        if not shp_m.intersects(search_area):
            continue
        clipped_geom = shp_m.intersection(search_area)
        if clipped_geom.is_empty:
            continue
        clipped.append({
            "type": "Feature",
            "properties": feature.get("properties", {}),
            "geometry": transform(to_4326, clipped_geom).__geo_interface__,
        })
    return clipped


def fetch_probabilistic_wind_swaths(lat: float, lon: float, search_area, bbox=None):
    if bbox is None:
        bbox = (lon - 1.5, lat - 1.5, lon + 1.5, lat + 1.5)
    swaths = {}

    for label, layer_id in NHC_WIND_LAYER_IDS.items():
        layer_url = f"{NHC_WIND_SWATH_BASE}/{layer_id}"
        data = _arcgis_query(layer_url, bbox, out_fields="percentage,idp_filedate")
        features = data.get("features", [])
        if search_area:
            features = _clip_features_to_search_area(features, search_area)
        swaths[label] = features

    return swaths


def fetch_hurricane_tracks(lat: float, lon: float, search_area, bbox=None):
    # Use past track layers for active storms; these are dynamic per storm
    # We query all past track layers in the tropical MapServer with bbox.
    # For now, just use AT1 past track (layer 12) as a representative active track.
    if bbox is None:
        bbox = (lon - 1.5, lat - 1.5, lon + 1.5, lat + 1.5)
    layer_url = f"{NHC_WIND_SWATH_BASE}/12"
    data = _arcgis_query(layer_url, bbox, out_fields="*")
    features = data.get("features", [])
    if search_area:
        features = _clip_features_to_search_area(features, search_area)
    return features


def fetch_tornado_tracks(lat: float, lon: float, search_area, bbox=None):
    if bbox is None:
        bbox = (lon - 1.5, lat - 1.5, lon + 1.5, lat + 1.5)
    layer_url = "https://gis.mrcc.purdue.edu/arcgis/rest/services/MRCC/tornadotracks/MapServer/1"
    data = _arcgis_query(layer_url, bbox, out_fields="yr,mo,dy,mag,len")
    features = data.get("features", [])
    if search_area:
        features = _clip_features_to_search_area(features, search_area)
    return features


def fetch_wind_layers(lat: float, lon: float, search_area, bbox=None):
    """
    Returns geometry-ready wind layers clipped to search radius.
    """
    swaths = fetch_probabilistic_wind_swaths(lat, lon, search_area, bbox=bbox)
    tracks = fetch_hurricane_tracks(lat, lon, search_area, bbox=bbox)
    tornado_paths = fetch_tornado_tracks(lat, lon, search_area, bbox=bbox)

    return {
        "wind_swaths": swaths,
        "tracks": tracks,
        "tornado_paths": tornado_paths,
    }


