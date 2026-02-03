import time

import requests
import streamlit as st

from shapely.geometry import (
    shape,
    Point,
)

FEMA_FEATURE_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)
FEMA_REPETITIVE_LOSS_BG_URL = (
    "https://services.arcgis.com/XG15cJAlne2vxtgt/arcgis/rest/services/"
    "Repetitive_Loss_Counts_(Block_Group)/FeatureServer/0/query"
)

FEMA_ZONE_EXPLANATIONS = {
    "AE": {
        "title": "High Flood Risk (Zone AE)",
        "summary": (
            "This area is within the 1% annual-chance floodplain "
            "(commonly called the 100-year floodplain)."
        ),
        "insurance": "Flood insurance is federally required for most mortgages.",
    },
    "A": {
        "title": "High Flood Risk (Zone A)",
        "summary": "High flood risk area without detailed base flood elevations.",
        "insurance": "Flood insurance is federally required.",
    },
    "VE": {
        "title": "Very High Flood Risk (Coastal Zone VE)",
        "summary": "Coastal area with wave action and storm surge risk.",
        "insurance": "Flood insurance is federally required and typically expensive.",
    },
    "X": {
        "title": "Low Flood Risk (Zone X)",
        "summary": (
            "Outside the 1% annual-chance floodplain. "
            "Represents minimal flood risk."
        ),
        "insurance": "Flood insurance is not federally required.",
    },
    "OPEN WATER": {
        "title": "Open Water / Water Body",
        "summary": "Permanent water features such as rivers or lakes.",
        "insurance": "Flood insurance requirements depend on structure placement.",
    },
}

zone_descriptions = {
    "AE": "higher-risk floodplains along major streams",
    "A": "higher-risk floodplains without detailed elevation studies",
    "D": "areas with undetermined flood risk",
    "X": "low-risk areas",
}

FLOOD_ZONE_COLORS = {
    "VE": {"stroke": "#7F0000", "fill": "#D32F2F", "opacity": 0.55},  # Extreme
    "AE": {"stroke": "#C62828", "fill": "#EF5350", "opacity": 0.50},  # High
    "A":  {"stroke": "#EF6C00", "fill": "#FFB74D", "opacity": 0.45},  # High (unstudied)
    "D":  {"stroke": "#6A1B9A", "fill": "#CE93D8", "opacity": 0.35},  # Undetermined
    "X":  {"stroke": "#1B5E20", "fill": "#A5D6A7", "opacity": 0.18},  # Low
}

REPETITIVE_LOSS_TOTAL_BUCKETS = [
    (1, 10, {"fill": "#BBDEFB", "stroke": "#1E88E5"}),
    (11, 50, {"fill": "#64B5F6", "stroke": "#1976D2"}),
    (51, 200, {"fill": "#2196F3", "stroke": "#1565C0"}),
    (201, float("inf"), {"fill": "#0D47A1", "stroke": "#0D47A1"}),
]

REPETITIVE_LOSS_UNMITIGATED_BUCKETS = [
    (1, 10, {"fill": "#FFCDD2", "stroke": "#E53935"}),
    (11, 50, {"fill": "#EF9A9A", "stroke": "#D32F2F"}),
    (51, 200, {"fill": "#E53935", "stroke": "#B71C1C"}),
    (201, float("inf"), {"fill": "#B71C1C", "stroke": "#7F0000"}),
]

@st.cache_data(show_spinner=False, ttl=86400)
def fetch_fema_flood_zones(bbox, page_size=50, max_pages=40):
    west, south, east, north = bbox

    all_features = []
    offset = 0

    for _ in range(max_pages):
        params = {
            "where": "1=1",
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": 4326,
            "outSR": 4326,
            "returnGeometry": "true",
            "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
            "resultRecordCount": page_size,
            "resultOffset": offset,
            "f": "geojson",
        }

        r = requests.get(FEMA_FEATURE_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        features = data.get("features", [])
        if not features:
            break

        all_features.extend(features)

        if not data.get("exceededTransferLimit"):
            break

        offset += page_size
        time.sleep(0.15)

    return {
        "type": "FeatureCollection",
        "features": all_features,
    }


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_fema_repetitive_loss_block_groups(bbox, page_size=2000):
    west, south, east, north = bbox

    params = {
        "where": "1=1",
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outSR": 4326,
        "returnGeometry": "true",
        "outFields": "geoid_bg,any_rl,any_rl_unmitigated,nfip_rl,nfip_srl,fma_rl,fma_srl",
        "resultRecordCount": page_size,
        "f": "geojson",
    }

    r = requests.get(FEMA_REPETITIVE_LOSS_BG_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    return {
        "type": "FeatureCollection",
        "features": data.get("features", []),
    }


def _repetitive_loss_bucket_style(count, buckets):
    for min_val, max_val, colors in buckets:
        if min_val <= count <= max_val:
            return {
                "color": colors["stroke"],
                "weight": 1.0,
                "fillColor": colors["fill"],
                "fillOpacity": 0.3,
            }

    return {
        "color": "#9E9E9E",
        "weight": 0.8,
        "fillOpacity": 0.1,
    }


def repetitive_loss_total_style(feature):
    count = feature.get("properties", {}).get("any_rl") or 0
    return _repetitive_loss_bucket_style(count, REPETITIVE_LOSS_TOTAL_BUCKETS)


def repetitive_loss_unmitigated_style(feature):
    count = feature.get("properties", {}).get("any_rl_unmitigated") or 0
    return _repetitive_loss_bucket_style(count, REPETITIVE_LOSS_UNMITIGATED_BUCKETS)


def flood_zone_at_point(geojson, lat, lon):
    """
    Returns the FLD_ZONE for the polygon containing the point, or None.
    """
    pt = Point(lon, lat)

    for feature in geojson.get("features", []):
        geom = feature.get("geometry")
        props = feature.get("properties", {})
        if not geom:
            continue

        polygon = shape(geom)
        if polygon.contains(pt):
            return props.get("FLD_ZONE"), props.get("SFHA_TF")

    return None, None


def flood_zone_style(feature):
    zone = feature["properties"].get("FLD_ZONE", "")
    base = FLOOD_ZONE_COLORS.get(zone, None)

    if base:
        return {
            "color": base["stroke"],
            "weight": 1.2,
            "fillColor": base["fill"],
            "fillOpacity": base["opacity"],
        }

    return {
        "color": "#9E9E9E",
        "weight": 0.6,
        "fillOpacity": 0.15,
    }