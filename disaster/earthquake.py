import time

import requests
import streamlit as st

USGS_QFAULTS_URL = (
    "https://earthquake.usgs.gov/arcgis/rest/services/haz/Qfaults/MapServer/21/query"
)


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_usgs_qfaults(bbox, page_size=2000, max_pages=3):
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
            "outFields": (
                "fault_name,section_name,age,slip_rate,slip_sense,"
                "mapped_certainty,symbology,total_fault_length"
            ),
            "resultRecordCount": page_size,
            "resultOffset": offset,
            "f": "geojson",
        }

        r = requests.get(USGS_QFAULTS_URL, params=params, timeout=30)
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


def earthquake_fault_style(_feature):
    return {
        "color": "#FF6F00",
        "weight": 2.2,
        "opacity": 0.85,
    }