import time
from dataclasses import dataclass
from datetime import date

import polyline
import streamlit as st

import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim

import os
from dotenv import load_dotenv

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun, azimuth

import requests
import pandas as pd

from PIL import Image, ImageDraw
import math
import io

from PIL import ImageFont

from shapely.geometry import shape, Point
from shapely.ops import transform
import pyproj

from collections import defaultdict

from lxml import etree
from shapely.geometry import Polygon

FEMA_FEATURE_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

WILDFIRE_KML_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/"
    "EDW/EDW_MTBS_01/MapServer/generateKML"
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


# ---------------------------------------------
# Load environment variables (.env)
# ---------------------------------------------
load_dotenv()

# -----------------------------
# Calculation models
# -----------------------------
@dataclass(frozen=True)
class MortgageInputs:
    home_price: float
    down_payment_value: float
    down_payment_is_percent: bool
    loan_term_years: int
    annual_interest_rate_pct: float
    start_month: int
    start_year: int

    include_costs: bool

    # Taxes & costs (we keep all internally normalized to monthly dollars)
    property_tax_value: float
    property_tax_is_percent: bool  # if percent, percent of home price per year
    home_insurance_annual: float
    pmi_monthly: float
    hoa_monthly: float
    other_monthly: float


def monthly_pi_payment(principal: float, annual_rate_pct: float, term_years: int) -> float:
    """
    Standard fixed-rate amortization payment:
      M = P * [ r(1+r)^n / ((1+r)^n - 1) ]
    where r = annual_rate/12, n = years*12.

    Bankrate explicitly publishes this form and defines r as annual/12. :contentReference[oaicite:3]{index=3}
    """
    if principal <= 0:
        return 0.0
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0
    if r == 0:
        return principal / n
    num = r * (1 + r) ** n
    den = (1 + r) ** n - 1
    return principal * (num / den)


def amortization_totals(principal: float, annual_rate_pct: float, term_years: int, payment: float) -> tuple[float, float]:
    """
    Compute total interest and total paid (P+I) using a month-by-month schedule with cent rounding.
    This avoids drift and better matches what calculators display.
    """
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0

    bal = principal
    total_interest = 0.0
    total_paid = 0.0

    for m in range(1, n + 1):
        if bal <= 0:
            break
        interest = round(bal * r, 2)
        principal_paid = round(payment - interest, 2)

        # If we're overpaying in the final month, clamp.
        if principal_paid > bal:
            principal_paid = round(bal, 2)
            payment_effective = round(principal_paid + interest, 2)
        else:
            payment_effective = round(payment, 2)

        bal = round(bal - principal_paid, 2)
        total_interest = round(total_interest + interest, 2)
        total_paid = round(total_paid + payment_effective, 2)

    return total_interest, total_paid


def compute_costs_monthly(inputs: MortgageInputs, method: str) -> dict:
    """
    Normalize costs to monthly amounts. Key difference between methods is *input cadence*.

    NerdWallet: tax & insurance are yearly; HOA & mortgage insurance are monthly. :contentReference[oaicite:4]{index=4}
    Bankrate: includes taxes/insurance/HOA in the monthly payment view; inputs are editable. :contentReference[oaicite:5]{index=5}
    """
    # Property tax monthly:
    if inputs.property_tax_is_percent:
        # percent of home price per year
        annual_tax = inputs.home_price * (inputs.property_tax_value / 100.0)
        property_tax_monthly = annual_tax / 12.0
    else:
        # dollar amount per year for both methods (we keep UI flexible)
        property_tax_monthly = inputs.property_tax_value / 12.0

    home_insurance_monthly = inputs.home_insurance_annual / 12.0

    # HOA and PMI handling:
    # Both Bankrate and NerdWallet treat HOA and PMI as monthly pass-through costs.
    # Differences between calculators are in input cadence and presentation, not math.
    hoa_monthly = inputs.hoa_monthly
    pmi_monthly = inputs.pmi_monthly

    other_monthly = inputs.other_monthly

    return {
        "property_tax_monthly": property_tax_monthly,
        "home_insurance_monthly": home_insurance_monthly,
        "hoa_monthly": hoa_monthly,
        "pmi_monthly": pmi_monthly,
        "other_monthly": other_monthly,
    }


def payoff_date(start_year: int, start_month: int, term_years: int) -> str:
    # payoff month is start + n-1 months (display only)
    n = term_years * 12
    y = start_year
    m = start_month
    m_total = (y * 12 + (m - 1)) + (n - 1)
    y2 = m_total // 12
    m2 = (m_total % 12) + 1
    return date(y2, m2, 1).strftime("%b. %Y")


def render_bankrate_math():
    st.markdown("""
### Bankrate-Style Mortgage Calculation

**Monthly Principal & Interest**

\[
M = P \times \frac{r(1+r)^n}{(1+r)^n - 1}
\]

Where:
- **P** = Loan principal  
- **r** = Annual interest rate ÷ 12  
- **n** = Loan term (years × 12)

**Assumptions**
- Fixed-rate mortgage
- Monthly compounding
- Cent-level rounding per payment
- Taxes, insurance, HOA added to monthly payment
- No ZIP-based tax estimation (user-supplied only)

**Notes**
- This matches Bankrate’s published amortization method.
- Extra payments supported in later phase.
""")


def render_nerdwallet_math():
    st.markdown("""
### NerdWallet-Style Mortgage Calculation

**Monthly Principal & Interest**

\[
M = P \\times \\frac{r(1+r)^n}{(1+r)^n - 1}
\]

Where:
- **P** = Loan principal  
- **r** = Annual interest rate ÷ 12  
- **n** = Loan term (years × 12)

**Assumptions**
- Fixed-rate mortgage
- Monthly compounding
- Cent-level rounding
- Property tax & homeowners insurance entered **annually**
- HOA & mortgage insurance entered **monthly**

**Notes**
- Matches NerdWallet’s cost-cadence behavior
- No automatic tax or insurance estimation
""")


def arm_delete(confirm_key):
    st.session_state[confirm_key] = True


def _get_loc_by_label(locations: list[dict], label: str) -> dict | None:
    for loc in locations:
        if loc["label"] == label:
            return loc
    return None


@st.cache_data(show_spinner=False)
def ors_directions_driving(
    api_key: str,
    start_lon: float,
    start_lat: float,
    end_lon: float,
    end_lat: float,
) -> tuple[float, float, list]:
    """
    Returns (distance_meters, duration_seconds) using ORS driving-car directions.
    Supports both ORS response formats and safely extracts metrics.
    """
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {
        "Content-Type": "application/json",
        "Authorization": api_key,
    }

    payload = {
        "coordinates": [
            [start_lon, start_lat],
            [end_lon, end_lat],
        ]
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    # ---------------------------------------
    # Parse ORS response (features or routes)
    # ---------------------------------------

    # GeoJSON-style response
    if "features" in data and data["features"]:
        props = data["features"][0].get("properties", {})
        summary = props.get("summary")
        segments = props.get("segments")

    # Classic routing response
    elif "routes" in data and data["routes"]:
        route = data["routes"][0]
        summary = route.get("summary")
        segments = route.get("segments")

    else:
        err_msg = data.get("error", {}).get("message", str(data))
        raise RuntimeError(f"OpenRouteService error: {err_msg}")

    # ---------------------------------------
    # Extract distance & duration safely
    # ---------------------------------------

    distance = summary.get("distance") if summary else None
    duration = summary.get("duration") if summary else None

    # Fallback: some ORS responses only populate segments
    if (distance is None or duration is None) and segments:
        distance = segments[0].get("distance")
        duration = segments[0].get("duration")

    if distance is None or duration is None:
        raise RuntimeError(
            "OpenRouteService response missing distance/duration "
            f"(summary={summary}, segments={segments})"
        )

    geometry = None

    if "features" in data:
        geometry = data["features"][0]["geometry"]["coordinates"]
    elif "routes" in data:
        geometry = data["routes"][0].get("geometry")

    return float(distance), float(duration), geometry


@st.cache_data(show_spinner=False)
def google_directions_driving(
    api_key: str,
    start: dict,
    end: dict,
    departure_dt,
) -> tuple[float, float, list]:
    """
    Returns (distance_meters, duration_seconds) using Google Routes API.
    Traffic-aware when departure_dt is provided.
    """
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "routes.duration,"
            "routes.distanceMeters,"
            "routes.polyline.encodedPolyline"
        ),
    }

    # ---------------------------------------
    # Ensure RFC3339 UTC timestamp (Z format)
    # ---------------------------------------
    if departure_dt.tzinfo is None:
        departure_dt = departure_dt.replace(tzinfo=timezone.utc)

    departure_ts = (
        departure_dt
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": start["lat"],
                    "longitude": start["lon"],
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": end["lat"],
                    "longitude": end["lon"],
                }
            }
        },
        "travelMode": "DRIVE",

        # Traffic-aware routing
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "departureTime": departure_ts,

        # REQUIRED context
        "languageCode": "en-US",
        "units": "METRIC",

        # REQUIRED IN PRACTICE (even if all false)
        "routeModifiers": {
            "avoidTolls": False,
            "avoidHighways": False,
            "avoidFerries": False,
        },
    }

    # ---------------------------------------
    # DEBUG: inspect exact payload sent to Google
    # (TEMPORARY — remove after verification)
    # ---------------------------------------
    # st.code(payload, language="json")

    payload = {k: v for k, v in payload.items() if v is not None}

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    route = data["routes"][0]

    # ---------------------------------------
    # Distance & duration may exist at
    # route-level OR leg-level (Google quirk)
    # ---------------------------------------

    distance = route.get("distanceMeters")
    duration = route.get("duration")

    # Fallback to first leg if needed
    if (distance is None or duration is None) and route.get("legs"):
        leg = route["legs"][0]
        distance = leg.get("distanceMeters")
        duration = leg.get("duration")

    if distance is None or duration is None:
        raise RuntimeError(
            "Google Routes response missing distance/duration: "
            f"{route}"
        )

    polyline = route.get("polyline", {}).get("encodedPolyline")

    return (
        float(distance),
        float(duration.rstrip("s")),
        polyline,
    )


def geocode_once(address: str) -> tuple[float, float]:
    geolocator = Nominatim(
        user_agent="house-planner-prototype",
        timeout=5,
    )
    location = geolocator.geocode(address)
    if not location:
        raise RuntimeError(f"Could not geocode address: {address}")
    return location.latitude, location.longitude


def decode_geometry(geometry, provider):
    """
    Returns list of (lat, lon)
    """
    if not geometry:
        return []

    if provider == "ORS":
        # ORS may return encoded polyline OR GeoJSON coordinates
        if isinstance(geometry, str):
            return polyline.decode(geometry)

        # GeoJSON-style [[lon, lat], ...]
        return [(lat, lon) for lon, lat in geometry]

    if provider == "GOOGLE":
        return polyline.decode(geometry)

    return []


@st.cache_data(show_spinner=False, ttl=86400)
def get_static_osm_image(lat, lon, zoom=19, size=800):
    tile_size = 256
    scale = size // tile_size

    def deg2num(lat_deg, lon_deg, zoom):
        lat_rad = math.radians(lat_deg)
        n = 2.0 ** zoom
        xtile = int((lon_deg + 180.0) / 360.0 * n)
        ytile = int(
            (1.0 - math.log(math.tan(lat_rad) + (1 / math.cos(lat_rad))) / math.pi)
            / 2.0 * n
        )
        return xtile, ytile

    cx, cy = deg2num(lat, lon, zoom)

    img = Image.new("RGB", (size, size))
    for dx in range(-scale // 2, scale // 2):
        for dy in range(-scale // 2, scale // 2):
            url = (
                "https://services.arcgisonline.com/ArcGIS/rest/services/"
                "World_Imagery/MapServer/tile/"
                f"{zoom}/{cy + dy}/{cx + dx}"
            )

            headers = {
                "User-Agent": "House-Planner-Prototype/1.0 (local use)",
            }

            resp = requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()

            tile = Image.open(io.BytesIO(resp.content))

            img.paste(
                tile,
                (
                    (dx + scale // 2) * tile_size,
                    (dy + scale // 2) * tile_size,
                ),
            )

    return img


def compute_season_azimuths(lat, lon, tz_name):
    seasons = {
        "Winter": date(2025, 12, 21),
        "Equinox": date(2025, 3, 20),
        "Summer": date(2025, 6, 21),
    }

    results = {}

    for season, d in seasons.items():
        loc = LocationInfo(latitude=lat, longitude=lon, timezone=tz_name)
        s = sun(loc.observer, date=d, tzinfo=ZoneInfo(tz_name))

        azimuths = []
        t = s["sunrise"]
        while t <= s["sunset"]:
            azimuths.append(
                azimuth(loc.observer, t)
            )
            t += timedelta(minutes=10)

        results[season] = azimuths

    return results


def draw_solar_overlay(base_img, azimuths_by_season, base_alpha=0.60):
    img = base_img.copy().convert("RGBA")
    draw = ImageDraw.Draw(img)

    size = img.size[0]
    cx = cy = size // 2
    r_inner = int(0.15 * size)
    r_outer = int(0.45 * size)

    # Fixed, legend-safe colors
    season_colors = {
        "Winter": (79, 195, 247),
        "Equinox": (129, 199, 132),
        "Summer": (255, 183, 77),
    }

    # ----------------------------------------
    # 1) Build dominant-season ownership per 5° bin
    # ----------------------------------------
    dominant_bins = {}

    for season, azimuths in azimuths_by_season.items():
        for a in azimuths:
            bin_angle = int(a // 5) * 5
            dominant_bins.setdefault(bin_angle, {})
            dominant_bins[bin_angle][season] = (
                dominant_bins[bin_angle].get(season, 0) + 1
            )

    # ----------------------------------------
    # 2) Render each bin ONCE, using dominant season
    # ----------------------------------------
    alpha = int(255 * base_alpha)

    for angle, season_counts in dominant_bins.items():
        dominant_season = max(season_counts, key=season_counts.get)
        color = (*season_colors[dominant_season], alpha)

        start_deg = angle - 90
        end_deg = angle + 5 - 90

        # Per-bin overlay
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        odraw = ImageDraw.Draw(overlay)

        # Outer wedge
        odraw.pieslice(
            [
                cx - r_outer,
                cy - r_outer,
                cx + r_outer,
                cy + r_outer,
            ],
            start_deg,
            end_deg,
            fill=color,
        )

        # Inner cutout mask
        mask = Image.new("L", img.size, 0)
        mdraw = ImageDraw.Draw(mask)
        mdraw.pieslice(
            [
                cx - r_inner,
                cy - r_inner,
                cx + r_inner,
                cy + r_inner,
            ],
            start_deg,
            end_deg,
            fill=255,
        )

        overlay.paste((0, 0, 0, 0), mask=mask)
        img.alpha_composite(overlay)

    # ----------------------------------------
    # 3) Reference ring (context, not data)
    # ----------------------------------------
    draw.ellipse(
        [
            cx - r_outer,
            cy - r_outer,
            cx + r_outer,
            cy + r_outer,
        ],
        outline=(255, 255, 255, int(255 * base_alpha * 0.15)),
        width=2,
    )

    # ----------------------------------------
    # 4) Compass (cardinal directions)
    # ----------------------------------------
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
    except IOError:
        font = ImageFont.load_default()
    compass_radius = r_inner - 18

    draw.text((cx - 6, cy - compass_radius), "N", fill="white", font=font)
    draw.text((cx - 6, cy + compass_radius - 14), "S", fill="white", font=font)
    draw.text((cx + compass_radius - 12, cy - 8), "E", fill="white", font=font)
    draw.text((cx - compass_radius + 4, cy - 8), "W", fill="white", font=font)

    return img


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


import xml.etree.ElementTree as ET
from zipfile import ZipFile
from io import BytesIO

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
    ns = {"kml": "http://www.opengis.net/kml/2.2"}
    root = etree.fromstring(kml_text.encode("utf-8"))

    polygons = []

    for placemark in root.findall(".//kml:Placemark", ns):
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

            if len(points) >= 4:
                try:
                    polygons.append(Polygon(points))
                except Exception:
                    pass

    return polygons


# -----------------------------
# Session State
# -----------------------------
if "map_data" not in st.session_state:
    default_locations = [
        {
            "label": "House",
            "address": "4005 Ancient Oak Ct, Annandale, VA 22003",
        },
        {
            "label": "Work",
            "address": "7500 GEOINT Dr, Springfield, VA 22150",
        },
        {
            "label": "Daycare",
            "address": "6935 Columbia Pike, Annandale, VA 22003",
        },
    ]

    locations = []
    for i, loc in enumerate(default_locations):
        lat, lon = geocode_once(loc["address"])
        locations.append({
            "label": loc["label"],
            "address": loc["address"],
            "lat": lat,
            "lon": lon,
        })

        # Be polite to Nominatim (1 request / second)
        if i < len(default_locations) - 1:
            time.sleep(1)

    st.session_state["map_data"] = {
        "locations": locations
    }

    st.session_state["map_badge"] = f"{len(locations)} locations"

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

method = st.selectbox(
    "Calculation method",
    ["Bankrate-style", "NerdWallet-style"],
    help="Affects input conventions and displayed assumptions."
)

if "mortgage_badge" not in st.session_state:
    st.session_state["mortgage_badge"] = "Monthly: —"

# -----------------------------
# Safe defaults for section badges
# -----------------------------
monthly_badge = "Monthly: —"
map_badge = "0 locations"
commute_badge = "—"

# =============================
# Mortgage Section
# =============================
with st.expander(
    f"Mortgage & Loan Assumptions  •  {st.session_state['mortgage_badge']}",
    expanded=st.session_state["mortgage_expanded"],
):

    with st.expander("Show the math & assumptions", expanded=False):
        if method == "Bankrate-style":
            render_bankrate_math()
        elif method == "NerdWallet-style":
            render_nerdwallet_math()

    # Layout: left input panel, right output panel
    left, right = st.columns([1.05, 1.25], gap="large")

    with left:
        with st.form("mortgage_form"):
            st.subheader("Modify the values and click Calculate")

            home_price = st.number_input(
                "Home Price ($)",
                min_value=0.0,
                value=400000.0,
                step=1000.0,
                format="%.2f"
            )

            dp_cols = st.columns([0.75, 0.25], gap="small")
            with dp_cols[0]:
                down_payment_value = st.number_input(
                    "Down Payment",
                    min_value=0.0,
                    value=20.0,
                    step=1.0,
                    label_visibility="collapsed"
                )
            with dp_cols[1]:
                down_payment_is_percent = st.selectbox(
                    "Unit",
                    ["%", "$"],
                    index=0,
                    label_visibility="collapsed"
                )

            dp_is_percent = (down_payment_is_percent == "%")

            loan_term_years = st.number_input(
                "Loan Term (years)",
                min_value=1,
                value=30,
                step=1
            )

            annual_rate = st.number_input(
                "Interest Rate (%)",
                min_value=0.0,
                value=6.17,
                step=0.01,
                format="%.2f"
            )

            sd_cols = st.columns([0.6, 0.4])
            with sd_cols[0]:
                start_month_name = st.selectbox(
                    "Start Date (month)",
                    ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
                    index=0
                )
            with sd_cols[1]:
                start_year = st.number_input(
                    "Start Date (year)",
                    min_value=1900,
                    max_value=2200,
                    value=2026,
                    step=1
                )

            start_month = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].index(start_month_name) + 1

            include_costs = st.checkbox("Include Taxes & Costs Below", value=True)

            st.markdown("### Annual Tax & Cost")

            tax_cols = st.columns([0.75, 0.25], gap="small")
            with tax_cols[0]:
                property_tax_value = st.number_input(
                    "Property Taxes",
                    min_value=0.0,
                    value=1.2,
                    step=0.1,
                    label_visibility="collapsed"
                )
            with tax_cols[1]:
                property_tax_unit = st.selectbox(
                    "Unit",
                    ["%", "$/year"],
                    index=0,
                    label_visibility="collapsed"
                )

            property_tax_is_percent = (property_tax_unit == "%")

            home_insurance_annual = st.number_input(
                "Home Insurance ($/year)",
                min_value=0.0,
                value=1500.0,
                step=50.0
            )

            pmi_monthly = st.number_input(
                "PMI / Mortgage Insurance ($/month)",
                min_value=0.0,
                value=0.0,
                step=10.0
            )

            hoa_monthly = st.number_input(
                "HOA Fee ($/month)",
                min_value=0.0,
                value=0.0,
                step=10.0
            )

            other_monthly = st.number_input(
                "Other Home Costs ($/month)",
                min_value=0.0,
                value=0.0,
                step=25.0,
                help="Home-related costs not captured above (maintenance, misc)."
            )

            # ---- Annual Tax & Cost Summary ----
            if property_tax_is_percent:
                property_tax_annual = home_price * (property_tax_value / 100.0)
            else:
                property_tax_annual = property_tax_value

            monthly_home_costs = (
                    property_tax_annual / 12.0
                    + home_insurance_annual / 12.0
                    + pmi_monthly
                    + hoa_monthly
                    + other_monthly
            )

            include_household_expenses = st.checkbox(
                "Include Household Expenses Below",
                value=True
            )

            # =============================
            # Household Expenses
            # =============================
            st.markdown("### Household Expenses")

            if include_household_expenses:
                daycare_monthly = st.number_input(
                    "Daycare ($/month)",
                    min_value=0.0,
                    value=0.0,
                    step=50.0
                )

                groceries_weekly = st.number_input(
                    "Groceries ($/week)",
                    min_value=0.0,
                    value=0.0,
                    step=10.0
                )

                utilities_monthly = st.number_input(
                    "Utilities ($/month)",
                    min_value=0.0,
                    value=0.0,
                    step=25.0
                )

                car_maintenance_annual = st.number_input(
                    "Car Maintenance ($/year)",
                    min_value=0.0,
                    value=0.0,
                    step=100.0
                )
            else:
                daycare_monthly = 0.0
                groceries_weekly = 0.0
                utilities_monthly = 0.0
                car_maintenance_annual = 0.0

            # ---- Household Expenses Summary ----
            household_monthly = (
                    daycare_monthly
                    + (groceries_weekly * 52.0 / 12.0)
                    + utilities_monthly
                    + (car_maintenance_annual / 12.0)
            )

            calculate = st.form_submit_button("Calculate", type="primary")

            if calculate:
                st.session_state["mortgage_expanded"] = True

        # =============================
        # Additional Custom Expenses
        # =============================
        st.markdown("### Additional Expenses")

        if "custom_expenses" not in st.session_state:
            st.session_state["custom_expenses"] = pd.DataFrame(
                columns=["Label", "Amount", "Cadence"]
            )

        with st.form("custom_expenses_form"):
            custom_df = st.data_editor(
                st.session_state["custom_expenses"],
                hide_index=True,
                num_rows="dynamic",
                column_config={
                    "Label": st.column_config.TextColumn("Expense"),
                    "Amount": st.column_config.NumberColumn(
                        "Amount",
                        min_value=0.0,
                        step=10.0
                    ),
                    "Cadence": st.column_config.SelectboxColumn(
                        "Cadence",
                        options=["$/month", "$/year"]
                    ),
                },
            )

            save_custom = st.form_submit_button("Apply Expenses")

        if save_custom:
            st.session_state["custom_expenses"] = custom_df

        if not st.session_state["custom_expenses"].empty:
            custom_monthly = 0.0
            for _, row in st.session_state["custom_expenses"].iterrows():
                if row["Cadence"] == "$/month":
                    custom_monthly += row["Amount"]
                elif row["Cadence"] == "$/year":
                    custom_monthly += row["Amount"] / 12.0

            st.caption(
                f"**Custom Expenses Summary:** "
                f"${custom_monthly:,.0f} / month"
            )

    # -----------------------------
    # RIGHT PANEL (computed outputs)
    # -----------------------------
    with right:
        if calculate:
            # ---- Loan Summary inputs ----
            if dp_is_percent:
                down_payment_amt = home_price * (down_payment_value / 100.0)
            else:
                down_payment_amt = down_payment_value

            loan_amount = max(home_price - down_payment_amt, 0.0)

            inputs = MortgageInputs(
                home_price=home_price,
                down_payment_value=down_payment_value,
                down_payment_is_percent=dp_is_percent,
                loan_term_years=int(loan_term_years),
                annual_interest_rate_pct=annual_rate,
                start_month=int(start_month),
                start_year=int(start_year),
                include_costs=include_costs,
                property_tax_value=property_tax_value,
                property_tax_is_percent=property_tax_is_percent,
                home_insurance_annual=home_insurance_annual,
                pmi_monthly=pmi_monthly,
                hoa_monthly=hoa_monthly,
                other_monthly=other_monthly,
            )

            pi = monthly_pi_payment(
                loan_amount,
                inputs.annual_interest_rate_pct,
                inputs.loan_term_years
            )

            total_interest, total_pi_paid = amortization_totals(
                loan_amount,
                inputs.annual_interest_rate_pct,
                inputs.loan_term_years,
                pi
            )

            costs = compute_costs_monthly(inputs, method=method)

            monthly_tax = costs["property_tax_monthly"] if include_costs else 0.0
            monthly_ins = costs["home_insurance_monthly"] if include_costs else 0.0
            monthly_hoa = costs["hoa_monthly"] if include_costs else 0.0
            monthly_pmi = costs["pmi_monthly"] if include_costs else 0.0
            monthly_other = costs["other_monthly"] if include_costs else 0.0

            monthly_total = (
                    pi
                    + monthly_tax
                    + monthly_ins
                    + monthly_hoa
                    + monthly_pmi
                    + monthly_other
            )

            # Green bar ONCE (after monthly_total exists)
            st.markdown(
                f"""
                <div style="padding: 14px; border-radius: 6px; background: #2e7d32;
                            color: white; font-size: 22px; font-weight: 700;">
                    Monthly Payment: ${monthly_total:,.2f}
                </div>
                """,
                unsafe_allow_html=True
            )

            # Update badge once
            st.session_state["mortgage_badge"] = f"Monthly: ${monthly_total:,.0f}"

            # ---- Summary ----
            st.markdown("### Summary")
            payoff = payoff_date(
                inputs.start_year,
                inputs.start_month,
                inputs.loan_term_years
            )

            custom_monthly = 0.0
            if not st.session_state.get("custom_expenses", pd.DataFrame()).empty:
                for _, row in st.session_state["custom_expenses"].iterrows():
                    if row["Cadence"] == "$/month":
                        custom_monthly += row["Amount"]
                    elif row["Cadence"] == "$/year":
                        custom_monthly += row["Amount"] / 12.0

            c1, c2, c3 = st.columns(3)
            with c1:
                st.metric("House Price", f"${home_price:,.2f}")
                st.metric("Loan Amount", f"${loan_amount:,.2f}")
                st.metric("Down Payment", f"${down_payment_amt:,.2f}")
            with c2:
                st.metric("Total of Mortgage Payments (P&I)", f"${total_pi_paid:,.2f}")
                st.metric("Total Interest", f"${total_interest:,.2f}")
                st.metric("Mortgage Payoff Date", payoff)
            with c3:
                st.metric(
                    "Tax & Cost (Monthly)",
                    f"${monthly_home_costs:,.0f}",
                    help="Property tax, insurance, HOA, PMI, other"
                )
                st.metric(
                    "Household Expenses (Monthly)",
                    f"${household_monthly:,.0f}",
                    help="Daycare, groceries, utilities, car"
                )
                st.metric(
                    "Additional Expenses (Monthly)",
                    f"${custom_monthly:,.0f}",
                    help="User-defined expenses (monthly + annual normalized)"
                )

# =============================
# Map Section
# =============================
with st.expander(
    f"Location Management  •  {st.session_state['map_badge']}",
    expanded=st.session_state["map_expanded"]
):
    st.subheader("Add a Location")

    geolocator = Nominatim(user_agent="house-planner-prototype")

    # -----------------------------
    # Add-location form (keeps UI stable)
    # -----------------------------
    with st.form("add_location_form"):
        cols = st.columns([0.25, 0.55, 0.2])

        with cols[0]:
            location_label = st.text_input(
                "Location Label",
                placeholder="House, Work, Daycare",
                label_visibility="collapsed"
            )

        with cols[1]:
            location_address = st.text_input(
                "Location Address",
                placeholder="Street, City, State",
                label_visibility="collapsed"
            )

        with cols[2]:
            submitted = st.form_submit_button("Add")

    # -----------------------------
    # Handle submission
    # -----------------------------
    if submitted:
        if not location_label or not location_address:
            st.warning("Please enter both a label and an address.")
        else:
            try:
                location = geolocator.geocode(location_address)
                if location:
                    st.session_state["map_data"]["locations"].append({
                        "label": location_label,
                        "address": location_address,
                        "lat": location.latitude,
                        "lon": location.longitude,
                    })

                    # Update badge and keep section open
                    count = len(st.session_state["map_data"]["locations"])
                    st.session_state["map_badge"] = f"{count} locations"
                else:
                    st.error("Address not found. Try a more complete address.")
            except Exception as e:
                st.error(f"Geocoding error: {e}")

    # -----------------------------
    # Build and render map + table
    # -----------------------------
    locations = st.session_state["map_data"]["locations"]

    table_col = st.columns([1])[0]

    with table_col:
        st.subheader("Locations")

        if not locations:
            st.caption("No locations added yet.")
        else:
            header_cols = st.columns([0.25, 0.55, 0.2])
            with header_cols[0]:
                st.markdown("**Label**")
            with header_cols[1]:
                st.markdown("**Address**")
            with header_cols[2]:
                st.markdown("**Delete**")

            st.divider()

            for idx, loc in enumerate(locations):
                row_cols = st.columns([0.25, 0.55, 0.2])

                with row_cols[0]:
                    st.write(loc["label"])

                with row_cols[1]:
                    st.caption(loc["address"])

                with row_cols[2]:
                    confirm_key = f"confirm_delete_{idx}"

                    if confirm_key not in st.session_state:
                        st.session_state[confirm_key] = False

                    if not st.session_state[confirm_key]:
                        st.button(
                            "Delete",
                            key=f"delete_{idx}",
                            type="secondary",
                            on_click=arm_delete,
                            args=(confirm_key,)
                        )
                    else:
                        if st.button(
                                "Confirm",
                                key=f"confirm_{idx}",
                                type="primary"
                        ):
                            st.session_state["map_data"]["locations"].pop(idx)
                            st.session_state.pop(confirm_key, None)

                            count = len(st.session_state["map_data"]["locations"])
                            st.session_state["map_badge"] = f"{count} locations"

                            st.rerun()

# =============================
# Commute Section
# =============================
with st.expander(
    "Commute Analysis",
    expanded=st.session_state["commute_expanded"]
):
    st.subheader("Trip Order (returns to Home of Record)")

    # ---------------------------------------------
    # Traffic & Routing Assumptions
    # ---------------------------------------------
    with st.expander("Traffic & Routing Assumptions", expanded=False):
        st.markdown(r"""
    ### Traffic Modeling (Current)

    This commute analysis uses **OpenRouteService – driving-car** routing.

    **What is modeled**
    - Average, non-time-specific driving speeds
    - Standard road hierarchy and turn costs
    - Deterministic routing (same inputs → same outputs)

    **What is NOT modeled**
    - Live traffic conditions
    - Rush-hour congestion
    - Day-of-week or time-of-day variation
    - Incidents, construction, or weather impacts

    **Implications**
    - Results represent a **baseline / typical commute**
    - Suitable for comparing route orderings and budgeting
    - Not suitable for predicting peak-hour delays

    **Upgrade Path**
    This section can be upgraded to a traffic-aware provider
    (e.g., Google Distance Matrix, TomTom, HERE) without
    changing the Trip Order UI or data model.
    """)

    locations = st.session_state.get("map_data", {}).get("locations", [])
    if not locations:
        st.info("Add locations in the Map section first (House, Work, Daycare, etc.).")
        st.stop()

    # ---------------------------------------------
    # Routing Method Selection
    # ---------------------------------------------
    routing_method = st.selectbox(
        "Routing Method",
        ["OpenRouteService (average traffic)", "Google (traffic-aware)"],
        help="Choose between average traffic (ORS) or traffic-aware routing (Google)."
    )

    # ---------------------------------------------
    # Routing API Keys (loaded from .env)
    # ---------------------------------------------
    ors_api_key = os.getenv("ORS_API_KEY")
    google_api_key = os.getenv("GOOGLE_MAPS_API_KEY")

    if routing_method.startswith("OpenRouteService"):
        if not ors_api_key:
            st.error(
                "ORS_API_KEY is not set. "
                "Add it to the .env file in the project root."
            )
            st.stop()
    else:
        if not google_api_key:
            st.error(
                "GOOGLE_MAPS_API_KEY is not set. "
                "Add it to the .env file in the project root."
            )
            st.stop()

    # ---------------------------------------------
    # Departure Time (used for traffic-aware routing)
    # ---------------------------------------------
    departure_time = st.time_input(
        "Departure Time (from Home)",
        value=pd.to_datetime("07:45").time(),
        help="Used for traffic-aware routing (Google only)."
    )

    # --- Choose Home of Record ---
    labels = [l["label"] for l in locations]
    home_label = st.selectbox(
        "Home of Record (start/end)",
        options=labels,
        index=labels.index("House") if "House" in labels else 0
    )
    home = _get_loc_by_label(locations, home_label)
    if not home:
        st.error("Home of record not found.")
        st.stop()

    # --- Editable table to choose stops + set order ---
    if "commute_table" not in st.session_state:
        # Initialize with everything excluded except non-home locations
        rows = []
        for loc in locations:
            if loc["label"] == home_label:
                continue
            rows.append({
                "Include": False,
                "Revisit": False,
                "Order": 1,
                "Loiter (min)": 0,
                "Label": loc["label"],
                "Address": loc["address"],
            })
        st.session_state["commute_table"] = pd.DataFrame(rows)

    # -------------------------------------------------
    # One-time sync of commute table with locations
    # (DO NOT mutate after editor is rendered)
    # -------------------------------------------------
    if "commute_table_synced" not in st.session_state:
        existing_labels = set(st.session_state["commute_table"]["Label"])

        new_rows = []
        for loc in locations:
            if loc["label"] == home_label:
                continue
            if loc["label"] not in existing_labels:
                new_rows.append({
                    "Include": False,
                    "Revisit": False,
                    "Order": 1,
                    "Loiter (min)": 0,
                    "Label": loc["label"],
                    "Address": loc["address"],
                })

        if new_rows:
            st.session_state["commute_table"] = pd.concat(
                [st.session_state["commute_table"], pd.DataFrame(new_rows)],
                ignore_index=True
            )

        st.session_state["commute_table_synced"] = True

    edited = st.data_editor(
        st.session_state["commute_table"],
        width="stretch",
        hide_index=True,
        column_config={
            "Include": st.column_config.CheckboxColumn("Include"),
            "Revisit": st.column_config.CheckboxColumn("Revisit"),
            "Order": st.column_config.NumberColumn("Order", min_value=1, step=1),
            "Loiter (min)": st.column_config.NumberColumn("Loiter (min)", min_value=0, step=5),
            "Label": st.column_config.TextColumn("Label", disabled=True),
            "Address": st.column_config.TextColumn("Address", disabled=True),
        },
    )

    # -------------------------------------------------
    # Build itinerary (ordered stops with optional revisit)
    # -------------------------------------------------

    # Only consider rows explicitly marked Include = True
    stops_df = edited[edited["Include"] == True].copy()
    if stops_df.empty:
        st.info(
            "Select at least one stop (Include = true), "
            "then set Order (1, 2, 3...)."
        )
        can_compute_commute = False
    else:
        can_compute_commute = True

    # Sort stops in visit order (deterministic)
    # Order is primary; Label breaks ties
    stops_df.sort_values(["Order", "Label"], inplace=True)

    # Resolve stops into location objects
    ordered_locs = []
    revisit_locs = []
    missing = []

    for _, row in stops_df.iterrows():
        label = row["Label"]

        # Look up the location from the Map section
        loc = _get_loc_by_label(locations, label)
        if not loc:
            missing.append(label)
            continue

        # First visit (always)
        ordered_locs.append(loc)

        # Optional second visit (e.g., daycare pickup)
        if row.get("Revisit", False):
            revisit_locs.append(loc)

    # Fail fast if any labels could not be resolved
    if missing:
        st.error(
            "These stops are missing from Map locations: "
            + ", ".join(missing)
        )
        st.stop()

    # -------------------------------------------------
    # Trigger route computation (persist results)
    # -------------------------------------------------
    compute = st.button("Compute Commute", type="primary")

    if compute and can_compute_commute:
        # -------------------------------------------------
        # Prepare route accumulation
        # -------------------------------------------------
        seg_rows = []
        total_m = 0.0  # meters
        total_s = 0.0  # seconds

        # Accumulate decoded route geometry
        all_route_points = []

        # Store per-segment routes for coloring
        segment_routes = []

        # Final route:
        #   Home → ordered stops → revisited stops → Home
        #
        # Example:
        #   Home → Daycare → Work → Daycare → Home
        points = [home] + ordered_locs + revisit_locs + [home]

        # -------------------------------------------------
        # Compute route segments via selected routing method
        # -------------------------------------------------
        spinner_label = (
            "Computing route segments (ORS)…"
            if routing_method.startswith("OpenRouteService")
            else "Computing route segments (Google, traffic-aware)…"
        )

        # # Initialize clock at departure time
        # today = pd.Timestamp.today().date()
        # current_dt = datetime.combine(today, departure_time)

        # Google traffic-aware routing only supports near-term departures
        now = datetime.now(tz=timezone.utc)

        candidate_dt = now.replace(
            hour=departure_time.hour,
            minute=departure_time.minute,
            second=0,
            microsecond=0,
        )

        # Google Routes REQUIRES departureTime >= now
        if candidate_dt <= now:
            candidate_dt = now + timedelta(minutes=1)

        current_dt = candidate_dt

        with st.spinner(spinner_label):
            for i in range(len(points) - 1):
                a = points[i]
                b = points[i + 1]

                if routing_method.startswith("OpenRouteService"):
                    dist_m, dur_s, geom = ors_directions_driving(
                        api_key=ors_api_key,
                        start_lon=a["lon"], start_lat=a["lat"],
                        end_lon=b["lon"], end_lat=b["lat"],
                    )

                    pts = decode_geometry(geom, "ORS")
                else:
                    dist_m, dur_s, geom = google_directions_driving(
                        api_key=google_api_key,
                        start=a,
                        end=b,
                        departure_dt=current_dt,
                    )

                    pts = decode_geometry(geom, "GOOGLE")

                if all_route_points and pts:
                    all_route_points.extend(pts[1:])  # avoid duplicate join point
                else:
                    all_route_points.extend(pts)

                # Persist this leg for map rendering
                segment_routes.append({
                    "from": a["label"],
                    "to": b["label"],
                    "points": pts,
                    "provider": (
                        "ORS"
                        if routing_method.startswith("OpenRouteService")
                        else "Google"
                    ),
                })

                arrive_dt = current_dt + timedelta(seconds=dur_s)

                # Loiter applies at destination (if defined)
                loiter_min = 0
                match = stops_df[stops_df["Label"] == b["label"]]
                if not match.empty:
                    loiter_min = int(match.iloc[0].get("Loiter (min)", 0))

                leave_dt = arrive_dt + timedelta(minutes=loiter_min)

                total_m += dist_m
                total_s += dur_s + (loiter_min * 60)

                seg_rows.append({
                    "From": a["label"],
                    "To": b["label"],
                    "Depart": current_dt.strftime("%H:%M"),
                    "Arrive": arrive_dt.strftime("%H:%M"),
                    "Drive (min)": round(dur_s / 60.0, 1),
                    "Loiter (min)": loiter_min,
                    "Leave": leave_dt.strftime("%H:%M"),
                    "Cumulative (min)": round(total_s / 60.0, 1),
                })

                # Advance clock
                current_dt = leave_dt

        # -------------------------------------------------
        # Persist results for re-render on rerun
        # -------------------------------------------------
        provider_key = (
            "ORS"
            if routing_method.startswith("OpenRouteService")
            else "Google"
        )

        st.session_state["commute_results"][provider_key] = {
            "segments": pd.DataFrame(seg_rows),
            "total_m": total_m,
            "total_s": total_s,
            "segment_routes": segment_routes,
        }

        # ---------------------------------------
        # Record most recent provider + request layer defaults
        # (apply just before checkboxes are created)
        # ---------------------------------------
        st.session_state["last_commute_provider"] = provider_key
        st.session_state["pending_layer_defaults"] = provider_key

        # Force rerun so checkboxes pick up state
        st.rerun()

    # -------------------------------------------------
    # Display persisted results (if available)
    # -------------------------------------------------
    if st.session_state.get("commute_results"):
        # Show results for the most recently computed provider
        provider_key = st.session_state.get("last_commute_provider")

        res = (
            st.session_state.get("commute_results", {})
            .get(provider_key)
        )

        if res:
            st.subheader(f"{provider_key} Commute Results")

            st.dataframe(
                res["segments"],
                width="stretch",
                hide_index=True
            )

            st.markdown("### Totals")
            st.metric(
                "Total Distance",
                f"{res['total_m'] / 1609.344:,.2f} mi"
            )
            st.metric(
                "Total Drive Time",
                f"{res['total_s'] / 60.0:,.1f} min"
            )

    if st.session_state.get("last_commute_provider"):
        st.subheader("Commute Map")

        # ---------------------------------------
        # Apply layer defaults exactly once per compute,
        # BEFORE widgets are instantiated
        # ---------------------------------------
        pending = st.session_state.pop("pending_layer_defaults", None)
        if pending == "ORS":
            st.session_state["show_ors"] = True
            st.session_state["show_google"] = False
            st.session_state["show_markers"] = True
        elif pending == "Google":
            st.session_state["show_google"] = True
            st.session_state["show_ors"] = False
            st.session_state["show_markers"] = True

        show_ors = st.checkbox("Show ORS routes", key="show_ors")
        show_google = st.checkbox("Show Google routes", key="show_google")
        show_markers = st.checkbox("Show depart / arrive markers", key="show_markers")

        m = folium.Map(
            location=[39.8283, -98.5795],
            zoom_start=4,
            tiles="OpenStreetMap"
        )

        bounds = []

        for loc in locations:
            bounds.append([loc["lat"], loc["lon"]])
            is_house = loc["label"].strip().lower() == "house"

            folium.Marker(
                location=[loc["lat"], loc["lon"]],
                popup=f"<b>{loc['label']}</b><br>{loc['address']}",
                icon=folium.Icon(
                    color="green" if is_house else "blue",
                    icon="home" if is_house else "info-sign"
                ),
            ).add_to(m)

        if bounds:
            m.fit_bounds(bounds)

        for provider, res in st.session_state["commute_results"].items():
            for seg in res.get("segment_routes", []):
                pts = seg["points"]
                if not pts:
                    continue

                show_route = (
                        (provider == "ORS" and show_ors)
                        or (provider == "Google" and show_google)
                )

                if show_route:
                    folium.PolyLine(
                        locations=pts,
                        color="#5E35B1" if provider == "ORS" else "#00695C",
                        weight=5,
                        opacity=0.85,
                        tooltip=f"{provider}: {seg['from']} → {seg['to']}",
                    ).add_to(m)

                if show_markers:
                    folium.CircleMarker(
                        location=pts[0],
                        radius=6,
                        color="#1565C0",
                        fill=True,
                        fill_color="#1565C0",
                    ).add_to(m)

                    folium.CircleMarker(
                        location=pts[-1],
                        radius=6,
                        color="#C62828",
                        fill=True,
                        fill_color="#C62828",
                    ).add_to(m)

        st_folium(m, width=900, height=500)

# =============================
# Sun & Light Analysis
# =============================
with st.expander(
    "☀️ Sun & Light Analysis",
    expanded=st.session_state["sun_expanded"],
):
    st.subheader("Annual Sun Exposure")

    with st.expander("ℹ️ How to read this chart"):
        st.markdown(
            """
    ### What this chart shows

    This diagram summarizes **the directions from which the sun reaches this property over the course of a year**.

    Each colored wedge represents a **compass direction** (north, east, south, west, etc.) where sunlight is present at some point during the day.
    
    The sun generally rises in the east, moves across the southern sky, and sets in the west, with the exact angle shifting slightly between winter and summer.

    ---

    ### How the sun paths are calculated

    - The house location is used to determine **local sunrise and sunset times**.
    - For three representative dates:
      - **Winter** (December 21)
      - **Equinox** (March 20)
      - **Summer** (June 21)
    - The sun’s position is sampled **every 10 minutes** while it is above the horizon.
    - Each sun position contributes to a **5° compass direction bin**.
    - For each direction, the season with the **most sun presence** becomes the dominant color.

    This ensures each direction is assigned to **one clear season**, avoiding confusing overlaps.

    ---

    ### What the colors mean

    - **Blue (Winter)** → Sun most often comes from this direction during Dec–Feb  
    - **Green (Equinox)** → Sun most often comes from this direction during spring & fall  
    - **Orange (Summer)** → Sun most often comes from this direction during Jun–Aug  

    The legend below the chart maps **months to seasons**.

    ---

    ### How to interpret gaps

    If part of the circle has **no color**, it means:
    - The sun **never rises above the horizon** in that direction at this location,
    - At any time of year.

    This is common for **north-facing directions** in the Northern Hemisphere.

    ---

    ### What this chart is (and is not)

    ✅ Shows **directional sun exposure trends**  
    ✅ Helps compare **front vs back vs side exposure**  
    ✅ Useful for understanding **seasonal lighting patterns**

    ❌ Does not show exact sunlight hours  
    ❌ Does not account for trees or buildings  
    ❌ Does not simulate shadows

    ---
    
    ### About imagery dates
    
    The aerial image shown is provided by a satellite imagery service and is part of a **composite map mosaic**.
    
    - The image is **not captured at a single moment in time**
    - Different parts of the image may come from **different capture dates**
    - Imagery is processed, corrected, and blended before being published
    
    Because of this, a single, exact “image date” does **not exist** and would be misleading to display.
    
    The imagery is used only for **visual context**.  
    All sun angle and seasonal calculations are based on **precise astronomical models** and the property’s geographic location.
    
    ---
    
    These refinements may be added in future versions.
    """
        )

    overlay_alpha = st.slider(
        "Overlay transparency",
        min_value=0.05,
        max_value=0.80,
        value=0.60,
        step=0.05,
        help="Controls visibility of seasonal sun coverage overlay.",
    )


    house = _get_loc_by_label(
        st.session_state["map_data"]["locations"],
        "House",
    )

    if house:
        tz_name = "America/New_York"  # derived implicitly for prototype

        base_img = get_static_osm_image(
            house["lat"],
            house["lon"],
            zoom=19,
            size=800,
        )

        azimuths = compute_season_azimuths(
            house["lat"],
            house["lon"],
            tz_name,
        )

        overlay = draw_solar_overlay(
            base_img,
            azimuths,
            base_alpha=overlay_alpha,
        )

        st.image(overlay, width="stretch")

        legend_cols = st.columns(3)

        with legend_cols[0]:
            st.markdown(
                "<span style='display:inline-block;width:16px;height:16px;"
                "background:#4FC3F7;border-radius:3px'></span> "
                "**Winter**<br>Dec, Jan, Feb",
                unsafe_allow_html=True,
            )

        with legend_cols[1]:
            st.markdown(
                "<span style='display:inline-block;width:16px;height:16px;"
                "background:#81C784;border-radius:3px'></span> "
                "**Equinox**<br>Mar–May, Sep–Nov",
                unsafe_allow_html=True,
            )

        with legend_cols[2]:
            st.markdown(
                "<span style='display:inline-block;width:16px;height:16px;"
                "background:#FFB74D;border-radius:3px'></span> "
                "**Summer**<br>Jun, Jul, Aug",
                unsafe_allow_html=True,
            )

        st.markdown(
            """
    **Seasonal Mapping**
    
    - **Winter** → Dec, Jan, Feb  
    - **Equinox** → Mar, Apr, May & Sep, Oct, Nov  
    - **Summer** → Jun, Jul, Aug
    """
        )

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
            st.checkbox("Earthquake fault proximity", key="hz_earthquake")
            st.checkbox("Hurricane / wind exposure", key="hz_wind")

        with c2:
            st.checkbox("Heat risk", key="hz_heat")
            st.checkbox("Historical disaster declarations", key="hz_disaster_history")
            st.checkbox("Previous land use history (County / Census)", key="hz_land_use")

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

            folium.LayerControl(
                collapsed=False,
                position="topright",
            ).add_to(m)

        if st.session_state.get("hz_wildfire"):
            if (
                    "wildfire_geoms" not in st.session_state
                    or st.session_state.get("wildfire_radius_key") != bbox_key
            ):
                kmz = fetch_mtbs_kmz(bbox)
                kml_text = extract_geometry_kml(kmz)

                geoms = parse_kml_geometries(kml_text)

                st.session_state["wildfire_geoms"] = geoms
                st.session_state["wildfire_radius_key"] = bbox_key

            wildfire_features = []

            to_3857 = pyproj.Transformer.from_crs(
                "EPSG:4326",
                "EPSG:3857",
                always_xy=True,
            ).transform

            for geom in st.session_state["wildfire_geoms"]:

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
                    "properties": {},
                    "geometry": transform(
                        pyproj.Transformer.from_crs(
                            "EPSG:3857", "EPSG:4326", always_xy=True
                        ).transform,
                        clipped,
                    ).__geo_interface__,
                })

            if wildfire_features:
                folium.GeoJson(
                    {
                        "type": "FeatureCollection",
                        "features": wildfire_features,
                    },
                    name="Historical Wildfires (MTBS)",
                    style_function=lambda _: {
                        "color": "#D84315",
                        "weight": 1.2,
                        "fillColor": "#FF8A65",
                        "fillOpacity": 0.35,
                    },
                    control=True,
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
                st.markdown(
                    f"""
        ### Historical Wildfire Context

        - **One or more historical wildfire perimeters intersect the area within {radius_miles} miles**
        - These represent **past burned areas**, not current fire conditions
        - Presence does **not** indicate future wildfire likelihood

        Wildfire data source: USGS / USDA MTBS
        """
                )
            else:
                st.markdown(
                    f"""
        ### Historical Wildfire Context

        - **No historical wildfire perimeters intersect the area within {radius_miles} miles**
        - This suggests **low recorded large-fire activity** in this region
        - Absence of recorded perimeters does **not guarantee zero wildfire risk**

        Wildfire data source: USGS / USDA MTBS
        """
                )

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

        if st.session_state["hz_wildfire"]:
            enabled.append("Wildfire risk")

        if st.session_state["hz_earthquake"]:
            enabled.append("Earthquake fault proximity")

        if st.session_state["hz_wind"]:
            enabled.append("Hurricane / wind exposure")

        if st.session_state["hz_heat"]:
            enabled.append("Heat risk")

        if st.session_state["hz_disaster_history"]:
            enabled.append("Historical disaster declarations")

        if st.session_state["hz_land_use"]:
            enabled.append("Previous land use history (County / Census-level)")

        for name in enabled:
            st.info(f"**{name}** is enabled — data integration will be wired in next.")
