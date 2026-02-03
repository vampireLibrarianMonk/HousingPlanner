import time

import requests
import streamlit as st

USGS_WBD_HUC12_URL = (
    "https://hydro.nationalmap.gov/arcgis/rest/services/wbd/MapServer/6/query"
)


def _fetch_huc12_bbox(bbox, page_size=2000, max_pages=4):
    west, south, east, north = bbox

    all_features = []
    offset = 0
    retry_page_size = page_size

    for _ in range(max_pages):
        params = {
            "where": "1=1",
            "geometry": f"{west},{south},{east},{north}",
            "geometryType": "esriGeometryEnvelope",
            "spatialRel": "esriSpatialRelIntersects",
            "inSR": 4326,
            "outSR": 4326,
            "returnGeometry": "true",
            "outFields": "huc12,name,hutype,areasqkm,states",
            "resultRecordCount": retry_page_size,
            "resultOffset": offset,
            "f": "geojson",
        }

        r = requests.get(USGS_WBD_HUC12_URL, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        features = data.get("features", [])
        if not features:
            break

        all_features.extend(features)

        if not data.get("exceededTransferLimit"):
            break

        offset += retry_page_size
        time.sleep(0.15)

        if retry_page_size > 500:
            retry_page_size = 500

    return all_features


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_usgs_huc12_watersheds(bbox, page_size=2000, max_pages=4):
    try:
        features = _fetch_huc12_bbox(bbox, page_size=page_size, max_pages=max_pages)
    except requests.HTTPError:
        west, south, east, north = bbox
        mid_lon = (west + east) / 2
        mid_lat = (south + north) / 2
        tiles = [
            (west, south, mid_lon, mid_lat),
            (mid_lon, south, east, mid_lat),
            (west, mid_lat, mid_lon, north),
            (mid_lon, mid_lat, east, north),
        ]

        features = []
        for tile in tiles:
            try:
                features.extend(_fetch_huc12_bbox(tile, page_size=500, max_pages=2))
            except requests.HTTPError:
                continue

    return {
        "type": "FeatureCollection",
        "features": features,
    }


WATERSHED_HUTYPE_STYLES = {
    "S": {"label": "Standard", "fill": "#4DD0E1", "stroke": "#006064"},
    "C": {"label": "Closed Basin", "fill": "#80DEEA", "stroke": "#00838F"},
    "F": {"label": "Frontal", "fill": "#B2EBF2", "stroke": "#0097A7"},
    "M": {"label": "Multiple Outlet", "fill": "#26C6DA", "stroke": "#006064"},
    "W": {"label": "Water", "fill": "#00ACC1", "stroke": "#004D40"},
    "I": {"label": "Island", "fill": "#81D4FA", "stroke": "#0277BD"},
    "U": {"label": "Urban", "fill": "#FFCC80", "stroke": "#EF6C00"},
    "D": {"label": "Indeterminate", "fill": "#CFD8DC", "stroke": "#546E7A"},
}


def watershed_style(feature):
    hutype = feature.get("properties", {}).get("hutype")
    style = WATERSHED_HUTYPE_STYLES.get(hutype)

    if not style:
        style = {"fill": "#B0BEC5", "stroke": "#455A64"}

    return {
        "color": style["stroke"],
        "weight": 1.2,
        "fillColor": style["fill"],
        "fillOpacity": 0.32,
    }