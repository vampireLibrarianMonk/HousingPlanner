import datetime

import requests
import streamlit as st
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from io import BytesIO
from shapely.geometry import Polygon

from bs4 import BeautifulSoup
from lxml import etree
import html

WILDFIRE_KML_URL = (
    "https://apps.fs.usda.gov/arcx/rest/services/"
    "EDW/EDW_MTBS_01/MapServer/generateKML"
)

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