from __future__ import annotations

from functools import lru_cache
import math
from typing import Iterable

import boto3
from botocore.exceptions import ClientError
import requests

from profile.costs import record_api_usage


@lru_cache(maxsize=1)
def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        raise RuntimeError(f"Unable to load secret '{secret_name}': {exc}")
    return resp["SecretString"]


def load_google_maps_api_key() -> str | None:
    try:
        return _get_secret("houseplanner/google_maps_api_key")
    except Exception:
        return None


def load_schooldigger_keys() -> tuple[str | None, str | None]:
    try:
        app_id = _get_secret("houseplanner/schooldigger_app_id")
    except Exception:
        app_id = None
    try:
        app_key = _get_secret("houseplanner/schooldigger_api_key")
    except Exception:
        app_key = None
    return app_id, app_key


def _normalize_school_types(types: Iterable[str] | None) -> list[str]:
    if not types:
        return []
    normalized = []
    for t in types:
        if not t:
            continue
        normalized.append(str(t).strip().lower())
    return normalized


def _school_level_from_types(types: Iterable[str] | None) -> str:
    normalized = _normalize_school_types(types)
    if any(t in normalized for t in ("primary_school", "elementary_school", "preschool")):
        return "Primary"
    if any(t in normalized for t in ("secondary_school", "high_school", "middle_school")):
        return "Secondary"
    if "school" in normalized:
        return "School"
    return "School"


def _distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def fetch_google_places_schools(
    *,
    api_key: str,
    lat: float,
    lon: float,
    radius_meters: int,
) -> list[dict]:
    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "places.id,"
            "places.displayName,"
            "places.formattedAddress,"
            "places.location,"
            "places.types,"
            "places.rating,"
            "places.userRatingCount,"
            "places.nationalPhoneNumber,"
            "places.websiteUri"
        ),
    }
    payload = {
        "includedTypes": ["school"],
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": radius_meters,
            }
        },
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    record_api_usage(
        service_key="Google Places API (New) Nearby Search Pro",
        url=url,
        requests=1,
        metadata={
            "provider": "google_places_new",
            "endpoint": "searchNearby",
            "lookup": "schools",
            "query": {
                "includedTypes": payload.get("includedTypes"),
                "locationRestriction": payload.get("locationRestriction"),
            },
        },
    )
    data = resp.json()
    return data.get("places", []) or []


def parse_google_school(place: dict, *, house_lat: float, house_lon: float) -> dict:
    location = place.get("location") or {}
    lat = location.get("latitude")
    lon = location.get("longitude")
    types = place.get("types") or []
    formatted_address = place.get("formattedAddress") or ""
    city = None
    state = None
    if formatted_address:
        parts = [p.strip() for p in formatted_address.split(",")]
        if len(parts) >= 3:
            city = parts[-3]
            state_tokens = parts[-2].split()
            if state_tokens:
                state = state_tokens[0]
        elif len(parts) == 2:
            city_tokens = parts[-1].split()
            if len(city_tokens) >= 2:
                city = " ".join(city_tokens[:-2]) or None
                state = city_tokens[-2]
    level = _school_level_from_types(types)
    distance = None
    if lat is not None and lon is not None:
        distance = _distance_miles(house_lat, house_lon, lat, lon)
    return {
        "source": "Google Places",
        "place_id": place.get("id"),
        "name": (place.get("displayName") or {}).get("text"),
        "address": formatted_address,
        "city": city,
        "state": state,
        "lat": lat,
        "lon": lon,
        "types": types,
        "level": level,
        "rating": place.get("rating"),
        "review_count": place.get("userRatingCount"),
        "phone": place.get("nationalPhoneNumber"),
        "website": place.get("websiteUri"),
        "distance_mi": distance,
    }


def fetch_schooldigger_schools(
    *,
    app_id: str,
    app_key: str,
    lat: float,
    lon: float,
    radius_miles: float,
    state: str,
    per_page: int = 50,
) -> list[dict]:
    """Fetch schools from SchoolDigger API using bounding box filtering.
    
    Note: The SchoolDigger API's nearLatitude/nearLongitude/distanceMiles parameters
    require Pro/Enterprise tier. For free tier, we use bounding box parameters which
    work correctly: boxLatitudeNW, boxLongitudeNW, boxLatitudeSE, boxLongitudeSE.
    """
    import math
    
    # Convert radius to lat/lon offsets for bounding box
    # 1 degree lat ≈ 69 miles
    # 1 degree lon varies with latitude: ≈ 69 * cos(lat) miles
    lat_offset = radius_miles / 69.0
    lon_offset = radius_miles / (69.0 * math.cos(math.radians(lat)))
    
    # Calculate bounding box corners (NW and SE)
    nw_lat = lat + lat_offset
    nw_lon = lon - lon_offset
    se_lat = lat - lat_offset
    se_lon = lon + lon_offset
    
    url = "https://api.schooldigger.com/v2.0/schools"
    params = {
        "st": state,
        "boxLatitudeNW": nw_lat,
        "boxLongitudeNW": nw_lon,
        "boxLatitudeSE": se_lat,
        "boxLongitudeSE": se_lon,
        "perPage": per_page,
        "appID": app_id,
        "appKey": app_key,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("schoolList", []) or []


def parse_schooldigger_school(school: dict) -> dict:
    address = school.get("address") or {}
    latlong = address.get("latLong") or {}
    level = school.get("schoolLevel")
    if isinstance(level, str):
        level = level.title()
    return {
        "schooldigger_id": school.get("schoolid"),
        "nces_id": school.get("schoolid"),
        "name": school.get("schoolName"),
        "city": address.get("city"),
        "state": address.get("state"),
        "phone": school.get("phone"),
        "url": school.get("url"),
        "level": level,
        "low_grade": school.get("lowGrade"),
        "high_grade": school.get("highGrade"),
        "is_charter": school.get("isCharterSchool"),
        "is_private": school.get("isPrivate"),
        "district_name": (school.get("district") or {}).get("districtName"),
        "rank_history": school.get("rankHistory") or [],
        "lat": latlong.get("latitude"),
        "lon": latlong.get("longitude"),
    }


def fetch_urban_institute_school(
    *,
    state: str,
    leaid: str,
    year: int = 2022,
) -> list[dict]:
    url = f"https://educationdata.urban.org/api/v1/schools/ccd/directory/{year}/"
    params = {"state": state, "leaid": leaid}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", []) or []


def fetch_urban_institute_schools_by_state(
    *,
    state: str,
    year: int = 2022,
    page: int = 1,
    per_page: int = 1000,
) -> list[dict]:
    url = f"https://educationdata.urban.org/api/v1/schools/ccd/directory/{year}/"
    params = {
        "state": state,
        "page": page,
        "per_page": per_page,
    }
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", []) or []


def fetch_nces_district_boundaries(*, state_fips: str) -> dict:
    url = (
        "https://nces.ed.gov/opengis/rest/services/"
        "School_District_Boundaries/EDGE_SCHOOLDISTRICT_TL23_SY2223/"
        "MapServer/0/query"
    )
    params = {
        "where": f"STATEFP='{state_fips}'",
        "outFields": "*",
        "f": "geojson",
    }
    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()
