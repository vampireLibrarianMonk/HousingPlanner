import time

import requests
import streamlit as st

SUPERFUND_BOUNDARIES_URL = (
    "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/"
    "FAC_Superfund_Site_Boundaries_EPA_Public/FeatureServer/0/query"
)

SUPERFUND_CIMC_POINTS_URL = (
    "https://services.arcgis.com/cJ9YHowT8TU7DUyn/arcgis/rest/services/"
    "Cleanups_in_my_Community_Sites/FeatureServer/0/query"
)

SUPERFUND_STATUS_STYLES = {
    "N": {"label": "Active", "color": "#D84315"},
    "Y": {"label": "Archived", "color": "#6D4C41"},
}


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_superfund_polygons(bbox, page_size=2000, max_pages=3):
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
            "outFields": "SITE_NAME,NPL_STATUS_CODE,EPA_ID,STATE_CODE,COUNTY,CITY_NAME,FEATURE_INFO_URL",
            "resultRecordCount": page_size,
            "resultOffset": offset,
            "f": "geojson",
        }

        r = requests.get(SUPERFUND_BOUNDARIES_URL, params=params, timeout=30)
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
def fetch_superfund_cimc_points(bbox, page_size=2000):
    west, south, east, north = bbox

    params = {
        "where": "SF_SITE_ID <> ''",
        "geometry": f"{west},{south},{east},{north}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": 4326,
        "outSR": 4326,
        "returnGeometry": "true",
        "outFields": (
            "SF_SITE_ID,SF_SITE_NAME,SF_ARCHIVED_IND,SF_NON_NPL_STATUS,"
            "STATE_CODE,CITY_NAME,COUNTY_NAME"
        ),
        "resultRecordCount": page_size,
        "f": "geojson",
    }

    r = requests.get(SUPERFUND_CIMC_POINTS_URL, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    return {
        "type": "FeatureCollection",
        "features": data.get("features", []),
    }


def superfund_polygon_style(_feature):
    return {
        "color": "#6A1B9A",
        "weight": 1.2,
        "fillColor": "#CE93D8",
        "fillOpacity": 0.25,
    }


def superfund_point_style(feature):
    archived = feature.get("properties", {}).get("SF_ARCHIVED_IND")
    style = SUPERFUND_STATUS_STYLES.get(archived) or {"color": "#C62828"}
    return {
        "color": style["color"],
        "fillColor": style["color"],
        "fillOpacity": 0.8,
        "radius": 5,
        "weight": 1,
    }