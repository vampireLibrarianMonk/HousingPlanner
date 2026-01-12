# ---------------------------------------------
# Global state
# ---------------------------------------------
from state import init_state
init_state()

# ---------------------------------------------
# Load environment variables (.env)
# ---------------------------------------------
from dotenv import load_dotenv
load_dotenv()

# =============================
# Standard library
# =============================
import io
import math
import time
import re
from collections import defaultdict
from datetime import (
    date,
    datetime,
    timedelta,
)
from zoneinfo import ZoneInfo
from zipfile import ZipFile
from io import BytesIO
import xml.etree.ElementTree as ET

# =============================
# Third-party: Core app & data
# =============================
import streamlit as st
import pandas as pd
import requests

# =============================
# Mapping & geospatial
# =============================
import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim

import pyproj
from shapely.geometry import (
    shape,
    Point,
    Polygon,
)
from shapely.ops import transform

# =============================
# Astronomy / solar analysis
# =============================
from astral import LocationInfo
from astral.sun import sun, azimuth

# =============================
# Imaging / rendering
# =============================
from PIL import (
    Image,
    ImageDraw,
    ImageFont,
)

from bs4 import BeautifulSoup
from lxml import etree
import html

from locations.logic import _get_loc_by_label

# =============================
# URLS
# =============================

FEMA_FEATURE_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

WILDFIRE_KML_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/"
    "EDW/EDW_MTBS_01/MapServer/generateKML"
)

# FEMA Open Data (v2) — county-level disaster declaration timeline
FEMA_DISASTER_DECLARATIONS_URL = (
    "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
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


def _state_abbrev_from_address_fallback(address: str) -> str | None:
    """
    Fallback: try to extract 'VA' from '..., VA 22003' style strings.
    """
    if not address:
        return None
    m = re.search(r",\s*([A-Z]{2})\s*\d{5}(-\d{4})?\s*$", address.strip())
    return m.group(1) if m else None


def _to_fema_designated_area_from_county(county: str) -> str:
    """
    Nominatim usually returns 'Fairfax County'. FEMA designatedArea uses:
      'Fairfax (County)'
    """
    c = (county or "").strip()
    c = re.sub(r"\s+County\s*$", "", c, flags=re.IGNORECASE)
    return f"{c} (County)" if c else ""


@st.cache_data(show_spinner=False, ttl=86400)
def reverse_geocode_county_state(lat: float, lon: float, address_fallback: str | None = None) -> tuple[str | None, str | None]:
    """
    Returns (county_name, state_abbrev) using Nominatim reverse-geocode.
    """
    geolocator = Nominatim(
        user_agent="house-planner-prototype",
        timeout=5,
    )
    loc = geolocator.reverse((lat, lon), language="en", exactly_one=True)
    if not loc:
        return None, _state_abbrev_from_address_fallback(address_fallback or "")

    raw = getattr(loc, "raw", {}) or {}
    addr = raw.get("address", {}) or {}

    county = addr.get("county") or addr.get("state_district")
    state_code = addr.get("state_code")  # often present in Nominatim
    if not state_code:
        state_code = _state_abbrev_from_address_fallback(address_fallback or "")

    return county, state_code


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_fema_disaster_declarations(
    state_abbrev: str,
    designated_area: str,
    top: int = 100,
) -> dict:
    """
    FEMA Open Data v2:
      /DisasterDeclarationsSummaries?$filter=state eq 'VA' and designatedArea eq 'Fairfax (County)'
    Returns the full JSON payload including metadata.
    """
    if not state_abbrev or not designated_area:
        return {"metadata": {"count": 0}, "DisasterDeclarationsSummaries": []}

    # NOTE: FEMA's API uses OData-style parameters ($filter, $orderby, $top)
    params = {
        "$filter": f"state eq '{state_abbrev}' and designatedArea eq '{designated_area}'",
        "$orderby": "declarationDate desc",
        "$top": int(top),
    }

    r = requests.get(FEMA_DISASTER_DECLARATIONS_URL, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def bbox_from_point(lat, lon, delta_lat=0.06, delta_lon=0.08):
    return (
        lon - delta_lon,
        lat - delta_lat,
        lon + delta_lon,
        lat + delta_lat,
    )


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


def flood_zone_style(feature):
    zone = feature["properties"].get("FLD_ZONE", "")

    if zone.startswith("A"):
        return {
            "color": "#1565C0",
            "weight": 1,
            "fillColor": "#1565C0",
            "fillOpacity": 0.45,
        }
    if zone.startswith("V"):
        return {
            "color": "#C62828",
            "weight": 1,
            "fillColor": "#C62828",
            "fillOpacity": 0.45,
        }
    if zone == "X":
        return {
            "color": "#2E7D32",
            "weight": 1,
            "fillColor": "#2E7D32",
            "fillOpacity": 0.25,
        }

    return {
        "color": "#9E9E9E",
        "weight": 0.5,
        "fillOpacity": 0.15,
    }


def summarize_flood_zones(geojson):
    zones = {
        f["properties"].get("FLD_ZONE")
        for f in geojson.get("features", [])
        if f.get("properties")
    }

    if not zones:
        return None, None

    # Risk priority (worst first)
    priority = ["VE", "AE", "A", "X", "OPEN WATER"]

    for p in priority:
        if p in zones:
            return p, zones

    return list(zones)[0], zones


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


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_mtbs_kmz(bbox):
    west, south, east, north = bbox

    params = {
        "LayerIDs": "63",        # Burned Area Boundaries (All Years)
        "Composite": "false",
        "FORMAT": "kmz",
        "BBOX": f"{west},{south},{east},{north}",
        "BBOXSR": "4326",
    }

    r = requests.get(
        "https://apps.fs.usda.gov/arcx/services/EDW/EDW_MTBS_01/MapServer/KmlServer",
        params=params,
        timeout=45,
    )
    r.raise_for_status()

    return r.content


def extract_geometry_kml(kmz_bytes):
    with ZipFile(BytesIO(kmz_bytes)) as z:
        # 1️⃣ Prefer doc.kml if present
        for name in z.namelist():
            if name.lower().endswith("doc.kml"):
                return z.read(name).decode("utf-8", errors="ignore")

        # 2️⃣ Otherwise, find KML that contains Placemark elements
        for name in z.namelist():
            if not name.lower().endswith(".kml"):
                continue

            text = z.read(name).decode("utf-8", errors="ignore")

            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                continue

            # Namespace-agnostic Placemark search
            if root.findall(".//{*}Placemark"):
                return text

    raise RuntimeError("No geometry KML found in KMZ")


def parse_kml_geometries(kml_text: str):
    """
    Parse MTBS KML into a list of dicts:

    [
        {
            "geometry": shapely.geometry.Polygon,
            "fire_year": int | None,
            "fire_name": str | None,
            "fire_id": str | None,
        },
        ...
    ]

    Geometry + metadata are bound per Placemark (GIS-safe).
    """
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    root = etree.fromstring(kml_text.encode("utf-8"))

    features = []

    # Extract metadata ONCE, in Placemark order
    metadata = extract_mtbs_fire_metadata(kml_text)

    placemarks = root.findall(".//kml:Placemark", ns)

    for placemark, meta in zip(placemarks, metadata):
        for poly in placemark.findall(".//kml:Polygon", ns):

            coords = poly.find(
                ".//kml:outerBoundaryIs/kml:LinearRing/kml:coordinates",
                ns,
            )
            if coords is None or not coords.text:
                continue

            points = []
            for coord in coords.text.strip().split():
                lon, lat, *_ = coord.split(",")
                points.append((float(lon), float(lat)))

            if len(points) < 4:
                continue

            try:
                geom = Polygon(points)
            except Exception:
                continue

            features.append({
                "geometry": geom,
                "fire_year": meta["fire_year"],
                "fire_name": meta["fire_name"],
                "fire_id": meta["fire_id"],
            })

    return features


def extract_mtbs_fire_metadata(kml_text: str):
    """
    Extract YEAR, FIRE_NAME, and FIRE_ID from MTBS Placemark <description> HTML.

    Returns a list of dicts aligned 1:1 with Placemark order:
    [
        {"fire_year": int | None, "fire_name": str | None, "fire_id": str | None},
        ...
    ]

    Read-only helper. Does NOT affect geometry parsing.
    """
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    root = etree.fromstring(kml_text.encode("utf-8"))

    results = []

    placemarks = root.findall(".//kml:Placemark", ns)
    for pm in placemarks:
        fire_year = None
        fire_name = None
        fire_id = None

        desc = pm.find("kml:description", ns)
        if desc is not None:
            html_text = html.unescape(
                etree.tostring(desc, method="html", encoding="unicode")
            )
            soup = BeautifulSoup(html_text, "html.parser")
            tds = soup.find_all("td")

            # MTBS description is a 2-column table: label td -> value td
            for i, td in enumerate(tds):
                label = td.get_text(strip=True)

                if i + 1 >= len(tds):
                    continue

                value = tds[i + 1].get_text(strip=True)

                if label == "YEAR":
                    try:
                        fire_year = int(value)
                    except ValueError:
                        pass

                elif label == "FIRE_NAME":
                    fire_name = value

                elif label == "FIRE_ID":
                    fire_id = value

        results.append({
            "fire_year": fire_year,
            "fire_name": fire_name,
            "fire_id": fire_id,
        })

    return results


def wildfire_recency_bucket(year: int) -> str:
    age = datetime.now().year - year
    if age <= 10:
        return "≤10 years"
    elif age <= 20:
        return "10–20 years"
    else:
        return ">20 years"


# -----------------------------
# Session State
# -----------------------------
if "map_badge" not in st.session_state:
    st.session_state["map_badge"] = "3 locations"

if "map_expanded" not in st.session_state:
    st.session_state["map_expanded"] = False

if "mortgage_expanded" not in st.session_state:
    st.session_state["mortgage_expanded"] = False

if "commute_results" not in st.session_state:
    # Holds results per provider: {"ORS": {...}, "Google": {...}}
    st.session_state["commute_results"] = {}

if "commute_expanded" not in st.session_state:
    st.session_state["commute_expanded"] = False

if "sun_expanded" not in st.session_state:
    st.session_state["sun_expanded"] = False

if "disaster_expanded" not in st.session_state:
    st.session_state["disaster_expanded"] = False

if "disaster_radius_miles" not in st.session_state:
    st.session_state["disaster_radius_miles"] = 5

if "show_ors" not in st.session_state:
    st.session_state["show_ors"] = False

if "show_google" not in st.session_state:
    st.session_state["show_google"] = False

if "show_markers" not in st.session_state:
    st.session_state["show_markers"] = False

if "hz_flood" not in st.session_state:
    st.session_state["hz_flood"] = False

if "hz_wildfire" not in st.session_state:
    st.session_state["hz_wildfire"] = False

if "hz_earthquake" not in st.session_state:
    st.session_state["hz_earthquake"] = False

if "hz_wind" not in st.session_state:
    st.session_state["hz_wind"] = False

if "hz_heat" not in st.session_state:
    st.session_state["hz_heat"] = False

if "hz_disaster_history" not in st.session_state:
    st.session_state["hz_disaster_history"] = False

if "hz_land_use" not in st.session_state:
    st.session_state["hz_land_use"] = False


# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="House Planner (Prototype)", layout="wide")

st.title("House Planner (Prototype)")

# -----------------------------
# Safe defaults for section badges
# -----------------------------
map_badge = "0 locations"
commute_badge = "—"

# =============================
# Mortgage Section
# =============================
from mortgage.ui import render_mortgage

if "mortgage_badge" not in st.session_state:
    st.session_state["mortgage_badge"] = "Monthly: —"

method = st.selectbox(
    "Calculation method",
    ["Bankrate-style", "NerdWallet-style"],
    help="Affects input conventions and displayed assumptions."
)

render_mortgage(method)

# =============================
# Location Management Section
# =============================
from locations.ui import render_locations

render_locations()

# =============================
# Commute Section
# =============================
from commute.ui import render_commute

render_commute()

# =============================
# Sun & Light Analysis
# =============================
from sun.ui import render_sun

render_sun()

# =============================
# Disaster Risk & Hazard Mapping
# =============================
with st.expander(
    "🌪️ Disaster Risk & Hazard Mapping",
    expanded=st.session_state["disaster_expanded"],
):
    st.subheader("Disaster Map (Home of Record)")

    locations = st.session_state["map_data"]["locations"]
    house = _get_loc_by_label(locations, "House")

    if not house:
        st.warning("Add a location labeled **House** to enable disaster mapping.")
    else:
        radius_miles = st.slider(
            "Search radius (miles)",
            min_value=1,
            max_value=50,
            step=1,
            key="disaster_radius_miles",
        )

        # ---------------------------------------
        # Planned hazard layers (toggle to enable)
        # ---------------------------------------
        st.markdown("### Planned hazard layers (toggle to enable)")

        c1, c2 = st.columns(2)

        with c1:
            st.checkbox("Flood zones (FEMA)", key="hz_flood")
            st.checkbox("Wildfire risk", key="hz_wildfire")
            st.checkbox("Historical disaster declarations", key="hz_disaster_history")

        with c2:
            st.checkbox("Heat risk", key="hz_heat")
            st.checkbox("Earthquake fault proximity", key="hz_earthquake")
            st.checkbox("Hurricane / wind exposure", key="hz_wind")

        st.divider()

        # ---------------------------------------
        # Disaster map
        # ---------------------------------------
        m = folium.Map(
            location=[house["lat"], house["lon"]],
            zoom_start=13,
            tiles="OpenStreetMap",
        )

        folium.Marker(
            location=[house["lat"], house["lon"]],
            popup=f"<b>House</b><br>{house['address']}",
            icon=folium.Icon(color="red", icon="home"),
        ).add_to(m)

        # ---------------------------------------
        # Build search radius geometry (single source of truth)
        # ---------------------------------------
        project = pyproj.Transformer.from_crs(
            "EPSG:4326", "EPSG:3857", always_xy=True
        ).transform

        house_point_m = transform(project, Point(house["lon"], house["lat"]))
        radius_meters = radius_miles * 1609.34

        # THIS must exist before any use
        search_area = house_point_m.buffer(radius_meters)

        # ---------------------------------------
        # Draw exact search radius (same geometry as clip)
        # ---------------------------------------
        search_area_latlon = transform(
            pyproj.Transformer.from_crs(
                "EPSG:3857", "EPSG:4326", always_xy=True
            ).transform,
            search_area,
        )

        folium.GeoJson(
            search_area_latlon.__geo_interface__,
            name=f"{radius_miles} mile search radius",
            style_function=lambda _: {
                "color": "#1565C0",
                "weight": 2,
                "fill": False,
                "dashArray": "6,6",
            },
            control=False,  # radius is informational, not a toggle
        ).add_to(m)

        # ---------------------------------------
        # FEMA Flood Hazard Zones (NFHL FeatureServer)
        # ---------------------------------------
        # NOTE:
        # FEMA flood data is cached per search radius.
        # Map panning/zooming does NOT trigger refetches.
        if not st.session_state.get("hz_flood"):
            st.session_state.pop("fema_flood_geojson", None)
            st.session_state.pop("fema_radius_key", None)

        if not st.session_state.get("hz_wildfire"):
            st.session_state.pop("wildfire_geoms", None)
            st.session_state.pop("wildfire_radius_key", None)

        if not st.session_state.get("hz_disaster_history"):
            st.session_state.pop("fema_disaster_history_last", None)

        # ---------------------------------------
        # Build radius-based bounding box (GIS-safe)
        # ---------------------------------------
        meters_per_degree_lat = 111_320
        meters_per_degree_lon = 111_320 * math.cos(math.radians(house["lat"]))

        delta_lat = radius_miles * 1609.34 / meters_per_degree_lat
        delta_lon = radius_miles * 1609.34 / meters_per_degree_lon

        bbox = (
            house["lon"] - delta_lon,
            house["lat"] - delta_lat,
            house["lon"] + delta_lon,
            house["lat"] + delta_lat,
        )

        bbox_key = (
            round(house["lat"], 4),
            round(house["lon"], 4),
            round(radius_miles, 2),
        )

        if st.session_state.get("hz_flood"):
            # ---------------------------------------
            # Cache FEMA fetch by radius ONLY
            # ---------------------------------------
            if (
                    "fema_flood_geojson" not in st.session_state
                    or st.session_state.get("fema_radius_key") != bbox_key
            ):
                st.session_state["fema_flood_geojson"] = fetch_fema_flood_zones(bbox)
                st.session_state["fema_radius_key"] = bbox_key

            flood_geojson = st.session_state["fema_flood_geojson"]

            zone_groups = defaultdict(list)

            # ---------------------------------------
            # Clip FEMA features to circular radius
            # ---------------------------------------
            zone_groups = defaultdict(list)

            for feature in flood_geojson["features"]:
                props = feature.get("properties")
                geom = feature.get("geometry")

                if not props or not geom:
                    continue

                polygon_m = transform(project, shape(geom))

                if polygon_m.intersects(search_area):
                    clipped_geom = polygon_m.intersection(search_area)

                    if not clipped_geom.is_empty:
                        zone = props.get("FLD_ZONE")
                        if zone:
                            clipped_feature = {
                                "type": "Feature",
                                "properties": props,
                                "geometry": transform(
                                    pyproj.Transformer.from_crs(
                                        "EPSG:3857", "EPSG:4326", always_xy=True
                                    ).transform,
                                    clipped_geom,
                                ).__geo_interface__,
                            }

                            zone_groups[zone].append(clipped_feature)

            for zone, features in zone_groups.items():
                folium.GeoJson(
                    {
                        "type": "FeatureCollection",
                        "features": features,
                    },
                    name=f"Flood Zone {zone}",
                    style_function=flood_zone_style,
                    tooltip=folium.GeoJsonTooltip(
                        fields=["FLD_ZONE", "SFHA_TF"],
                        aliases=["Flood Zone", "SFHA"],
                        sticky=True,
                    ),
                    control=True,  # enables legend toggle
                    show=True if zone != "X" else False,  # hide Zone X by default
                ).add_to(m)

        if st.session_state.get("hz_wildfire"):
            # ---------------------------------------
            # Fetch + cache wildfire items
            # ---------------------------------------
            if (
                    "wildfire_items" not in st.session_state
                    or st.session_state.get("wildfire_radius_key") != bbox_key
            ):
                kmz = fetch_mtbs_kmz(bbox)
                kml_text = extract_geometry_kml(kmz)

                # 🔑 geometry + metadata together (single source of truth)
                wildfire_items = parse_kml_geometries(kml_text)

                # st.caption(
                #     "DEBUG MTBS wildfire items sample: "
                #     f"{wildfire_items[:3]}"
                # )

                st.session_state["wildfire_items"] = wildfire_items
                st.session_state["wildfire_radius_key"] = bbox_key

            # ---------------------------------------
            # Build clipped wildfire features
            # ---------------------------------------
            wildfire_features = []

            to_3857 = pyproj.Transformer.from_crs(
                "EPSG:4326", "EPSG:3857", always_xy=True
            ).transform

            to_4326 = pyproj.Transformer.from_crs(
                "EPSG:3857", "EPSG:4326", always_xy=True
            ).transform

            for item in st.session_state["wildfire_items"]:
                geom = item["geometry"]
                fire_year = item["fire_year"]
                fire_name = item["fire_name"]
                fire_id = item["fire_id"]

                if fire_year is None:
                    continue

                geom_m = transform(to_3857, geom)

                if geom_m.is_empty:
                    continue

                if not geom_m.is_valid:
                    geom_m = geom_m.buffer(0)

                if not geom_m.intersects(search_area):
                    continue

                clipped = geom_m.intersection(search_area)

                if clipped.is_empty:
                    continue

                wildfire_features.append({
                    "type": "Feature",
                    "properties": {
                        "fire_year": fire_year,
                        "fire_name": fire_name,
                        "fire_id": fire_id,
                    },
                    "geometry": transform(to_4326, clipped).__geo_interface__,
                })

            # ---------------------------------------
            # Group wildfire features by fire year
            # ---------------------------------------
            wildfire_by_year = defaultdict(list)

            for feature in wildfire_features:
                wildfire_by_year[
                    feature["properties"]["fire_year"]
                ].append(feature)

            # ---------------------------------------
            # Add one toggleable layer per fire year
            # ---------------------------------------
            if wildfire_by_year:
                newest_year = max(wildfire_by_year)

                for year, features in sorted(wildfire_by_year.items()):
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": features,
                        },
                        name=f"Wildfires – {year}",
                        style_function=lambda _: {
                            "color": "#D84315",
                            "weight": 1.4,
                            "dashArray": "4,4",
                            "fillColor": "#FF8A65",
                            "fillOpacity": 0.22,
                        },
                        tooltip=folium.GeoJsonTooltip(
                            fields=["fire_name", "fire_year", "fire_id"],
                            aliases=["Fire Name", "Year", "MTBS ID"],
                            sticky=True,
                        ),
                        control=True,
                        show=(year == newest_year),  # newest on by default
                    ).add_to(m)

            # ---------------------------------------
            # Wildfire legend (map overlay)
            # ---------------------------------------
            if wildfire_features:
                wildfire_legend_html = """
                <div style="
                    position: fixed;
                    bottom: 35px;
                    left: 35px;
                    z-index: 9999;
                    background: white;
                    padding: 10px 14px;
                    border-radius: 6px;
                    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
                    font-size: 13px;
                ">
                    <b>Historical Wildfires (MTBS)</b><br>
                    <span style="
                        display:inline-block;
                        width:14px;
                        height:14px;
                        background:#FF8A65;
                        border:1px solid #D84315;
                        margin-right:6px;">
                    </span>
                    Burned Area Perimeter<br>
                    <span style="font-size:11px;color:#555;">
                        Historical large fires only
                    </span>
                </div>
                """

                m.get_root().html.add_child(
                    folium.Element(wildfire_legend_html)
                )

        folium.LayerControl(
            collapsed=False,
            position="topright",
        ).add_to(m)

        st_folium(
            m,
            width=900,
            height=500,
            returned_objects=[],
        )

        if st.session_state.get("hz_flood"):
            st.caption("Flood data source: FEMA National Flood Hazard Layer (NFHL)")

        st.divider()
        st.markdown("### Enabled layers:")

        enabled = []

        flood_geojson = st.session_state.get("fema_flood_geojson")

        if st.session_state.get("hz_flood") and flood_geojson and flood_geojson["features"]:
            house_zone, house_sfha = flood_zone_at_point(
                flood_geojson,
                house["lat"],
                house["lon"],
            )

            if house_zone:
                zone_info = FEMA_ZONE_EXPLANATIONS.get(house_zone)

                if zone_info:
                    st.markdown(
                        f"""
        ### {zone_info['title']}

        - **Summary:** {zone_info['summary']}
        - **Flood insurance:** {zone_info['insurance']}
        - **SFHA:** {"Yes" if house_sfha == "T" else "No"}
        - **Determination:** Flood zone determined at the house location.
        """,
                        unsafe_allow_html=True,
                    )

        if st.session_state.get("hz_wildfire"):
            if wildfire_features:
                fire_years = [
                    f["properties"]["fire_year"]
                    for f in wildfire_features
                    if f["properties"].get("fire_year") is not None
                ]

                most_recent_year = max(fire_years) if fire_years else "unknown"
                fire_count = len(wildfire_features)

                st.markdown(
                    f"""
        ### Historical Wildfire Context

        - **{fire_count} historical wildfire perimeter(s)** intersect the area within **{radius_miles} miles**
        - **Most recent recorded fire year:** {most_recent_year}
        - These represent **past burned areas**, not current fire conditions
        - Presence does **not** indicate future wildfire likelihood

        Wildfire data source: USGS / USDA MTBS
        """
                )
            else:
                st.markdown(
                    f"""
        ### Historical Wildfire Context

        - **No mapped historical wildfire perimeters** intersect the area within **{radius_miles} miles**
        - This reflects **recorded large-fire history only**
        - Absence of recorded perimeters does **not guarantee zero wildfire risk**

        Wildfire data source: USGS / USDA MTBS
        """
                )

        if st.session_state.get("hz_disaster_history"):
            county, state_abbrev = reverse_geocode_county_state(
                house["lat"],
                house["lon"],
                address_fallback=house.get("address"),
            )

            designated_area = _to_fema_designated_area_from_county(county or "")

            data = fetch_fema_disaster_declarations(
                state_abbrev=state_abbrev or "",
                designated_area=designated_area,
                top=100,
            )

            meta = data.get("metadata", {}) or {}
            rows = data.get("DisasterDeclarationsSummaries", []) or []

            if rows:
                st.markdown(
                    f"""
### Historical Disaster Declarations (County-Level)

- **County (FEMA designatedArea):** {designated_area}
- **State:** {state_abbrev}
- **Records returned:** {len(rows)}
- **Sort:** declarationDate (newest → oldest)
"""
                )

                df = pd.DataFrame(rows)

                # Keep a clean “buyer-readable” view, while retaining raw metadata in an expander
                keep_cols = [
                    "declarationDate",
                    "femaDeclarationString",
                    "declarationType",
                    "incidentType",
                    "declarationTitle",
                    "incidentBeginDate",
                    "incidentEndDate",
                    "disasterCloseoutDate",
                    "fyDeclared",
                    "paProgramDeclared",
                    "iaProgramDeclared",
                    "ihProgramDeclared",
                    "hmProgramDeclared",
                    "designatedArea",
                    "state",
                    "disasterNumber",
                ]

                existing = [c for c in keep_cols if c in df.columns]
                df_view = df[existing].copy()

                # Ensure newest-first (even if API changes behavior)
                if "declarationDate" in df_view.columns:
                    df_view["declarationDate"] = pd.to_datetime(df_view["declarationDate"], errors="coerce")
                    df_view = df_view.sort_values("declarationDate", ascending=False)
                    df_view["declarationDate"] = df_view["declarationDate"].dt.date.astype(str)

                # Light cleanup for date columns
                for c in ["incidentBeginDate", "incidentEndDate", "disasterCloseoutDate"]:
                    if c in df_view.columns:
                        d = pd.to_datetime(df_view[c], errors="coerce")
                        df_view[c] = d.dt.date.astype("string")

                st.dataframe(df_view, width="stretch", hide_index=True)

                with st.expander("FEMA API metadata (debug)", expanded=False):
                    st.json(meta)
            else:
                st.markdown(
                    f"""
### Historical Disaster Declarations (County-Level)

- **County (FEMA designatedArea):** {designated_area or "Unknown"}
- **State:** {state_abbrev or "Unknown"}
- **Result:** No records returned from FEMA for this county filter.
"""
                )

                nearby_zones = set()

        nearby_zones = set()

        flood_geojson = st.session_state.get("fema_flood_geojson")

        if flood_geojson:
            for feature in flood_geojson["features"]:
                props = feature.get("properties")
                geom = feature.get("geometry")

                if not props or not geom:
                    continue

                polygon_m = transform(project, shape(geom))

                # True spatial test (not distance-only)
                if polygon_m.intersects(search_area):
                    zone = props.get("FLD_ZONE")
                    if zone:
                        nearby_zones.add(zone)

        nearby_zones = sorted(nearby_zones)

        descriptions = [
            f"{zone_descriptions[z]} (Zone {z})"
            for z in nearby_zones
            if z in zone_descriptions and z != house_zone
        ]

        if descriptions:
            st.markdown(
                f"""
            <strong>Nearby flood risk context (within {radius_miles} miles):</strong><br>
            Surrounding areas include {", ".join(sorted(set(descriptions)))}.<br><br>
            These nearby flood zones do not change the flood classification at this property.
            """,
                unsafe_allow_html=True,
            )

        if st.session_state["hz_earthquake"]:
            enabled.append("Earthquake fault proximity")

        if st.session_state["hz_wind"]:
            enabled.append("Hurricane / wind exposure")

        if st.session_state["hz_heat"]:
            enabled.append("Heat risk")

        for name in enabled:
            st.info(f"**{name}** is enabled — data integration will be wired in next.")
