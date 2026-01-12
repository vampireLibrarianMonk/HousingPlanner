import re

import requests
import streamlit as st
from geopy.geocoders import Nominatim


FEMA_FEATURE_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

# FEMA Open Data (v2) — county-level disaster declaration timeline
FEMA_DISASTER_DECLARATIONS_URL = (
    "https://www.fema.gov/api/open/v2/DisasterDeclarationsSummaries"
)

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