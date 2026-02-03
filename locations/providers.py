import logging
import time
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError
from geopy.geocoders import Nominatim
import requests

logger = logging.getLogger(__name__)


def _normalize_address(address: str) -> str:
    normalized = " ".join(address.strip().split())
    lowered = normalized.lower()
    if "usa" not in lowered and "united states" not in lowered:
        normalized = f"{normalized}, USA"
    return normalized


def _fallback_queries(address: str) -> list[str]:
    if not address:
        return []
    tokens = [token.strip() for token in address.split(",") if token.strip()]
    # Only generate queries that include the street address to avoid vague matches
    if len(tokens) >= 3:
        city_state_zip = ", ".join(tokens[-2:])
        return [address, f"{tokens[0]}, {city_state_zip}"]
    return [address]


@lru_cache(maxsize=1)
def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        raise RuntimeError(f"Unable to load secret '{secret_name}': {exc}")
    logger.info("Loaded secret '%s'", secret_name)
    return resp["SecretString"]


def _geocode_google(address: str) -> tuple[float, float] | None:
    try:
        api_key = _get_secret("houseplanner/google_maps_api_key")
    except Exception as exc:
        logger.warning("Google geocoding unavailable: %s", exc)
        return None

    if not api_key:
        logger.warning("Google geocoding skipped: missing API key.")
        return None
    logger.info("Using Google geocoding for address: '%s'", address)

    url = "https://maps.googleapis.com/maps/api/geocode/json"
    try:
        resp = requests.get(url, params={"address": address, "key": api_key}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("Google geocoding request failed: %s", exc)
        return None

    data = resp.json()
    status = data.get("status")
    if status != "OK" or not data.get("results"):
        logger.warning("Google geocoding failed: status=%s", status)
        return None
    location = data["results"][0]["geometry"]["location"]
    return float(location["lat"]), float(location["lng"])


def _geocode_ors(address: str) -> tuple[float, float] | None:
    try:
        api_key = _get_secret("houseplanner/ors_api_key")
    except Exception as exc:
        logger.warning("ORS geocoding unavailable: %s", exc)
        return None

    if not api_key:
        logger.warning("ORS geocoding skipped: missing API key.")
        return None
    logger.info("Using ORS geocoding for address: '%s'", address)

    url = "https://api.openrouteservice.org/geocode/search"
    headers = {"Authorization": api_key}
    try:
        resp = requests.get(
            url,
            headers=headers,
            params={"text": address, "size": 1},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("ORS geocoding request failed: %s", exc)
        return None

    data = resp.json()
    features = data.get("features") or []
    if not features:
        logger.warning("ORS geocoding failed: no features returned")
        return None
    coords = features[0].get("geometry", {}).get("coordinates")
    if not coords or len(coords) < 2:
        logger.warning("ORS geocoding failed: invalid geometry %s", coords)
        return None
    return float(coords[1]), float(coords[0])


def geocode_once(address: str) -> tuple[float, float]:
    geolocator = Nominatim(
        user_agent="house-planner-prototype",
        timeout=10,
    )
    normalized = _normalize_address(address)
    logger.info("Geocoding address '%s' normalized to '%s'", address, normalized)
    queries = _fallback_queries(normalized)

    for query in queries:
        logger.warning("Nominatim query attempt: '%s'", query)
        try:
            start = time.monotonic()
            location = geolocator.geocode(query, timeout=10)
            elapsed = time.monotonic() - start
            logger.info("Nominatim query '%s' completed in %.2fs", query, elapsed)
        except Exception as exc:
            logger.error("Nominatim geocode error for '%s': %s", query, exc)
            location = None
        if location:
            logger.info(
                "Nominatim success for '%s': lat=%s lon=%s",
                query,
                location.latitude,
                location.longitude,
            )
            return location.latitude, location.longitude
        logger.warning("Nominatim failed for query: '%s'", query)

    logger.warning("Finished Nominatim attempts. Proceeding to ORS fallback.")

    logger.warning("Nominatim failed for all queries. Trying ORS geocoding.")
    for query in queries:
        logger.warning("ORS query attempt: '%s'", query)
        location = _geocode_ors(query)
        if location:
            logger.info(
                "ORS success for '%s': lat=%s lon=%s",
                query,
                location[0],
                location[1],
            )
            return location
        logger.warning("ORS failed for query: '%s'", query)

    logger.warning("ORS failed for all queries. Trying Google geocoding.")
    for query in queries:
        logger.warning("Google query attempt: '%s'", query)
        location = _geocode_google(query)
        if location:
            logger.info(
                "Google success for '%s': lat=%s lon=%s",
                query,
                location[0],
                location[1],
            )
            return location
        logger.warning("Google failed for query: '%s'", query)
    logger.error("Geocoding failed across Nominatim, Google, and ORS.")
    raise RuntimeError(f"Could not geocode address: {address}")
