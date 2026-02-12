"""Service availability providers for FCC broadband and delivery locations.

Security notes:
- FCC credentials loaded from AWS Secrets Manager (preferred) or env vars
- API keys never logged or exposed in error messages
- Zipfile extraction validates paths to prevent ZipSlip attacks
"""
from __future__ import annotations

import json
import logging
import math
import os
from functools import lru_cache
from pathlib import Path
import time
from typing import Any
import zipfile

import boto3
from botocore.exceptions import ClientError
import requests
import streamlit as st

logger = logging.getLogger(__name__)

# FCC API endpoints
FCC_AVAILABILITY_URL = "https://broadbandmap.fcc.gov/nbm/map/api/public/availability"
FCC_BLOCK_URL = "https://broadbandmap.fcc.gov/nbm/map/api/public/census-block"
FCC_GEOCODE_URL = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
FCC_BDC_BASE_URL = "https://bdc.fcc.gov/api/public"
FCC_LIST_DATES_URL = f"{FCC_BDC_BASE_URL}/map/listAsOfDates"

# Request timeouts (seconds)
_TIMEOUT_SHORT = 20
_TIMEOUT_MEDIUM = 30
_TIMEOUT_LONG = 120


@lru_cache(maxsize=8)
def _get_secret(secret_name: str) -> str:
    """Load secret from AWS Secrets Manager with in-memory cache.
    
    Note: Cache is bounded to 8 entries to limit memory exposure.
    In production, consider using a TTL cache for credential rotation.
    """
    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        # Don't leak secret name in logs if it contains sensitive info
        logger.error("Failed to load secret: %s", exc.response.get("Error", {}).get("Code"))
        raise RuntimeError(f"Unable to load secret '{secret_name}'") from exc
    return resp["SecretString"]


def load_google_maps_api_key() -> str | None:
    """Load Google Maps API key using the Commute module convention."""
    try:
        return _get_secret("houseplanner/google_maps_api_key")
    except Exception:
        return None


def load_fcc_credentials() -> tuple[str | None, str | None]:
    """Load FCC username + API token stored in AWS Secrets Manager."""
    try:
        username = _get_secret("houseplanner/fcc_username")
    except Exception:
        username = None
    try:
        token = _get_secret("houseplanner/fcc_api_key")
    except Exception:
        token = None
    if not username:
        username = _load_env_value("FCC_USERNAME")
    if not token:
        token = _load_env_value("FCC_API_KEY")
    return username, token


def _load_env_value(key: str) -> str | None:
    value = os.getenv(key)
    if value:
        return value
    env_path = Path("app/.env")
    if not env_path.exists():
        return None
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        env_key, env_value = line.split("=", 1)
        if env_key.strip() == key:
            return env_value.strip()
    return None


def _fcc_auth_headers() -> dict[str, str]:
    username, token = load_fcc_credentials()
    if not username or not token:
        return {}
    return {
        "username": username,
        "hash_value": token,
    }


def _require_fcc_headers() -> dict[str, str]:
    headers = _fcc_auth_headers()
    if not headers:
        raise RuntimeError(
            "FCC credentials missing. Set houseplanner/fcc_username and "
            "houseplanner/fcc_api_key in AWS Secrets Manager or FCC_USERNAME/FCC_API_KEY in app/.env."
        )
    return headers


def _distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _request_json(
    url: str,
    *,
    params: dict[str, Any],
    timeout: int = 30,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    backoffs = [1, 3]
    last_exc: Exception | None = None
    request_headers = {"User-Agent": "HousePlanner/1.0"}
    if headers:
        request_headers.update(headers)
    for attempt in range(len(backoffs) + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout, headers=request_headers)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict):
                return data
            raise RuntimeError(f"Unexpected JSON payload type: {type(data)}")
        except Exception as exc:
            last_exc = exc
            if attempt >= len(backoffs):
                break
            time.sleep(backoffs[attempt])
    message = f"Request failed for {url}"
    if last_exc:
        message = f"{message}: {last_exc}"
    raise RuntimeError(message) from last_exc


def _get_coords_from_address(address: str) -> tuple[float, float] | None:
    data = _request_json(
        FCC_GEOCODE_URL,
        params={
            "address": address,
            "benchmark": "Public_AR_Current",
            "format": "json",
        },
        timeout=30,
    )
    matches = (data.get("result") or {}).get("addressMatches") or []
    if not matches:
        return None
    coords = matches[0].get("coordinates") or {}
    lon = coords.get("x")
    lat = coords.get("y")
    if lat is None or lon is None:
        return None
    return float(lat), float(lon)


def _infer_tier(provider: dict[str, Any]) -> tuple[str, int]:
    tech = (provider.get("technology") or "").lower()
    max_down = provider.get("maxDownloadSpeed") or 0
    if "fiber" in tech and max_down >= 1000:
        return "Fiber", 5
    if "cable" in tech and max_down >= 300:
        return "Cable", 3
    if "dsl" in tech:
        return "DSL", 2
    return "Satellite", 1


def _summary_from_providers(providers: list[dict[str, Any]]) -> dict[str, Any]:
    if not providers:
        return {}
    scored = []
    for provider in providers:
        tech, score = _infer_tier(provider)
        scored.append(
            (
                score,
                provider.get("maxDownloadSpeed") or 0,
                provider,
                tech,
            )
        )
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = scored[0]
    best_provider = best[2]
    best_tech = best[3]
    return {
        "tech_tier": best_tech,
        "score": best[0],
        "best_provider": best_provider.get("providerName"),
        "max_download_mbps": best_provider.get("maxDownloadSpeed"),
        "max_upload_mbps": best_provider.get("maxUploadSpeed"),
    }


@st.cache_data(show_spinner=False, ttl=3600)
def test_fcc_credentials() -> dict[str, Any]:
    headers = _fcc_auth_headers()
    if not headers:
        return {
            "ok": False,
            "error": (
                "FCC credentials missing. Store houseplanner/fcc_username and "
                "houseplanner/fcc_api_key in AWS Secrets Manager."
            ),
        }
    try:
        data = _request_json(
            FCC_LIST_DATES_URL,
            params={},
            timeout=20,
            headers=headers,
        )
    except Exception as exc:
        return {"ok": False, "error": f"FCC credential test failed: {exc}"}
    return {"ok": True, "data": data}


def fetch_fcc_bdc_as_of_dates() -> dict[str, Any]:
    headers = _require_fcc_headers()
    return _request_json(
        FCC_LIST_DATES_URL,
        params={},
        timeout=20,
        headers=headers,
    )


def fetch_fcc_bdc_availability_list(
    *,
    as_of_date: str,
    category: str | None = None,
    technology_type: str | None = None,
) -> dict[str, Any]:
    headers = _require_fcc_headers()
    params: dict[str, Any] = {}
    if category:
        params["category"] = category
    if technology_type:
        params["technology_type"] = technology_type
    return _request_json(
        f"{FCC_BDC_BASE_URL}/map/downloads/listAvailabilityData/{as_of_date}",
        params=params,
        timeout=30,
        headers=headers,
    )


def download_fcc_bdc_file(
    *,
    file_id: int,
    file_type: int = 2,
    output_path: str | None = None,
) -> dict[str, Any]:
    headers = _require_fcc_headers()
    url = f"{FCC_BDC_BASE_URL}/map/downloads/downloadFile/availability/{file_id}/{file_type}"
    if not output_path:
        output_path = f"/tmp/fcc_downloads/bdc_{file_id}_type{file_type}.zip"
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    resp = requests.get(url, headers=headers, timeout=_TIMEOUT_LONG, stream=True)
    content_disp = resp.headers.get("content-disposition")
    content_type = resp.headers.get("content-type")
    if resp.status_code != 200:
        error_text = None
        try:
            error_text = resp.text[:1000]
        except Exception:
            error_text = None
        return {
            "ok": False,
            "status_code": resp.status_code,
            "content_disposition": content_disp,
            "content_type": content_type,
            "error": error_text,
        }

    with path.open("wb") as handle:
        for chunk in resp.iter_content(chunk_size=16384):
            if not chunk:
                continue
            handle.write(chunk)
    size = path.stat().st_size
    return {
        "ok": True,
        "status_code": resp.status_code,
        "content_disposition": content_disp,
        "content_type": content_type,
        "output_path": str(path),
        "bytes": size,
    }


def _is_safe_path(base: Path, target: Path) -> bool:
    """Check if target path is safely within base directory (prevents ZipSlip)."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def unzip_fcc_bdc_file(*, zip_path: str, extract_dir: str | None = None) -> dict[str, Any]:
    """Extract zip file with ZipSlip protection.
    
    Security: Validates all extracted paths stay within target directory.
    """
    path = Path(zip_path)
    if not path.exists():
        return {"ok": False, "error": f"Zip file not found: {zip_path}"}
    if not extract_dir:
        extract_dir = str(path.with_suffix(""))
    target = Path(extract_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.namelist()
            extracted_files = []
            for member in members:
                member_path = target / member
                # ZipSlip protection: reject paths that escape target directory
                if not _is_safe_path(target, member_path):
                    logger.warning("Skipping unsafe zip entry: %s", member)
                    continue
                # Extract single member safely
                archive.extract(member, target)
                extracted_files.append(str(member_path))
    except zipfile.BadZipFile as exc:
        return {"ok": False, "error": f"Invalid zip file: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": f"Extraction failed: {exc}"}
    
    return {"ok": True, "extract_dir": str(target), "files": extracted_files}


def preview_gpkg_layers(*, gpkg_path: str) -> dict[str, Any]:
    path = Path(gpkg_path)
    if not path.exists():
        return {"ok": False, "error": f"GeoPackage not found: {gpkg_path}"}
    try:
        import fiona
        import geopandas as gpd
    except Exception as exc:
        return {"ok": False, "error": f"GeoPandas/Fiona not available: {exc}"}
    try:
        layers = fiona.listlayers(path)
    except Exception as exc:
        return {"ok": False, "error": f"Unable to list layers: {exc}"}
    preview = []
    for layer in layers:
        try:
            gdf = gpd.read_file(path, layer=layer, rows=5)
            preview.append(
                {
                    "layer": layer,
                    "feature_count_sample": len(gdf),
                    "bounds": gdf.total_bounds.tolist() if not gdf.empty else None,
                    "columns": list(gdf.columns),
                }
            )
        except Exception as exc:
            preview.append({"layer": layer, "error": str(exc)})
    return {"ok": True, "layers": preview}


def run_gpkg_overlay_for_address(
    *,
    gpkg_path: str,
    address: str,
) -> dict[str, Any]:
    path = Path(gpkg_path)
    if not path.exists():
        return {"ok": False, "error": f"GeoPackage not found: {gpkg_path}"}
    coords = _get_coords_from_address(address)
    if not coords:
        return {"ok": False, "error": "Failed to geocode address."}
    lat, lon = coords
    try:
        import fiona
        import geopandas as gpd
        from shapely.geometry import Point
    except Exception as exc:
        return {"ok": False, "error": f"GeoPandas/Shapely not available: {exc}"}
    try:
        layers = fiona.listlayers(path)
    except Exception as exc:
        return {"ok": False, "error": f"Unable to list layers: {exc}"}
    if not layers:
        return {"ok": False, "error": "No layers found in GeoPackage."}
    layer = layers[0]
    try:
        gdf = gpd.read_file(path, layer=layer)
    except Exception as exc:
        return {"ok": False, "error": f"Unable to read layer {layer}: {exc}"}
    if gdf.empty:
        return {"ok": False, "error": f"Layer {layer} has no features."}
    point = Point(lon, lat)
    try:
        mask = gdf.contains(point)
    except Exception:
        mask = gdf.geometry.contains(point)
    matches = gdf[mask]
    return {
        "ok": True,
        "address": address,
        "lat": lat,
        "lon": lon,
        "layer": layer,
        "match_count": int(matches.shape[0]),
        "sample_columns": list(matches.columns)[:10],
    }


def load_gpkg_features_for_radius(
    *,
    gpkg_path: str,
    lat: float,
    lon: float,
    radius_miles: float,
) -> dict[str, Any]:
    """Load GeoPackage features that intersect with a circular search radius.

    Returns clipped features as GeoJSON FeatureCollection for map rendering.
    """
    path = Path(gpkg_path)
    if not path.exists():
        return {"ok": False, "error": f"GeoPackage not found: {gpkg_path}"}
    try:
        import fiona
        import geopandas as gpd
        import pyproj
        from shapely.geometry import Point, mapping
        from shapely.ops import transform
    except Exception as exc:
        return {"ok": False, "error": f"GeoPandas/Shapely/Pyproj not available: {exc}"}
    try:
        layers = fiona.listlayers(path)
    except Exception as exc:
        return {"ok": False, "error": f"Unable to list layers: {exc}"}
    if not layers:
        return {"ok": False, "error": "No layers found in GeoPackage."}

    # Build search area in meters (EPSG:3857)
    to_3857 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
    to_4326 = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
    house_point_m = transform(to_3857, Point(lon, lat))
    radius_meters = radius_miles * 1609.34
    search_area = house_point_m.buffer(radius_meters)
    # BBox in WGS84 for quick spatial index filtering
    search_area_4326 = transform(to_4326, search_area)
    bbox = search_area_4326.bounds

    all_features = []
    layer_stats = []

    for layer_name in layers:
        try:
            gdf = gpd.read_file(path, layer=layer_name)
        except Exception as exc:
            layer_stats.append({"layer": layer_name, "error": str(exc)})
            continue

        if gdf.empty:
            layer_stats.append({"layer": layer_name, "feature_count": 0, "clipped_count": 0})
            continue

        # Ensure CRS is WGS84
        if gdf.crs is None:
            gdf = gdf.set_crs("EPSG:4326")
        elif gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        original_count = len(gdf)
        clipped_features = []

        # Spatial index filter by bbox to reduce candidates
        try:
            sindex = gdf.sindex
            candidate_idx = list(sindex.intersection(bbox))
            gdf_candidates = gdf.iloc[candidate_idx]
        except Exception:
            gdf_candidates = gdf

        if gdf_candidates.empty:
            layer_stats.append(
                {
                    "layer": layer_name,
                    "feature_count": original_count,
                    "clipped_count": 0,
                    "columns": [c for c in gdf.columns if c != "geometry"],
                }
            )
            continue

        # Vectorized transform + clip in EPSG:3857
        try:
            gdf_m = gdf_candidates.to_crs("EPSG:3857")
        except Exception:
            gdf_m = gdf_candidates

        gdf_m = gdf_m.copy()
        # Fix invalid geometries (avoid SettingWithCopyWarning)
        gdf_m.loc[:, "geometry"] = gdf_m["geometry"].buffer(0)
        mask = gdf_m.intersects(search_area)
        gdf_hit = gdf_m.loc[mask].copy()

        if gdf_hit.empty:
            layer_stats.append(
                {
                    "layer": layer_name,
                    "feature_count": original_count,
                    "clipped_count": 0,
                    "columns": [c for c in gdf.columns if c != "geometry"],
                }
            )
            continue

        gdf_hit.loc[:, "geometry"] = gdf_hit["geometry"].intersection(search_area)
        gdf_hit = gdf_hit.loc[~gdf_hit["geometry"].is_empty].copy()

        # Back to WGS84
        try:
            gdf_hit = gdf_hit.to_crs("EPSG:4326")
        except Exception:
            pass

        for _, row in gdf_hit.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            props = {k: v for k, v in row.items() if k != "geometry"}
            for k, v in props.items():
                if hasattr(v, "item"):
                    props[k] = v.item()
                elif not isinstance(v, (str, int, float, bool, type(None))):
                    props[k] = str(v)
            clipped_features.append(
                {
                    "type": "Feature",
                    "properties": props,
                    "geometry": mapping(geom),
                }
            )

        layer_stats.append({
            "layer": layer_name,
            "feature_count": original_count,
            "clipped_count": len(clipped_features),
            "columns": [c for c in gdf.columns if c != "geometry"],
        })
        all_features.extend(clipped_features)

    return {
        "ok": True,
        "lat": lat,
        "lon": lon,
        "radius_miles": radius_miles,
        "layers": layer_stats,
        "total_features": len(all_features),
        "geojson": {
            "type": "FeatureCollection",
            "features": all_features,
        },
    }


def load_gpkg_features_for_radius_cached(
    *,
    gpkg_path: str,
    file_id: int,
    lat: float,
    lon: float,
    radius_miles: float,
    cache_dir: str = "/tmp/fcc_cache",
) -> dict[str, Any]:
    """Load GeoPackage features with persistent disk cache by file_id + location + radius."""
    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_key = f"{file_id}_{lat:.4f}_{lon:.4f}_{radius_miles:.2f}"
    cache_path = cache_root / f"{cache_key}.json"

    if cache_path.exists():
        try:
            cached = json.loads(cache_path.read_text())
            cached["cached"] = True
            cached["file_id"] = file_id
            cached["cache_path"] = str(cache_path)
            return cached
        except Exception:
            pass

    result = load_gpkg_features_for_radius(
        gpkg_path=gpkg_path,
        lat=lat,
        lon=lon,
        radius_miles=radius_miles,
    )
    result["cached"] = False
    result["file_id"] = file_id
    result["cache_path"] = str(cache_path)
    try:
        cache_path.write_text(json.dumps(result))
    except Exception:
        pass
    return result


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_fcc_broadband(
    *,
    lat: float | None,
    lon: float | None,
    address: str | None = None,
) -> dict[str, Any]:
    if (lat is None or lon is None) and address:
        coords = _get_coords_from_address(address)
        if coords:
            lat, lon = coords
    if lat is None or lon is None:
        return {}

    headers = _fcc_auth_headers()
    payload = _request_json(
        FCC_AVAILABILITY_URL,
        params={"latitude": lat, "longitude": lon},
        timeout=75,
        headers=headers or None,
    )
    providers = payload.get("serviceProviders") or []

    block_payload = _request_json(
        FCC_BLOCK_URL,
        params={"latitude": lat, "longitude": lon},
        timeout=60,
        headers=headers or None,
    )

    return {
        "serviceProviders": providers,
        "blockFips": block_payload.get("blockFips"),
        "summary": _summary_from_providers(providers),
    }


@st.cache_data(show_spinner=False, ttl=86400)
def fetch_delivery_locations(
    *,
    api_key: str,
    lat: float,
    lon: float,
    radius_meters: int = 5000,
) -> list[dict[str, Any]]:
    url = "https://maps.googleapis.com/maps/api/place/nearbysearch/json"
    carriers = {
        "USPS": "USPS",
        "UPS": "UPS Store",
        "FedEx": "FedEx Office",
        "DHL": "DHL",
        "Amazon Locker": "Amazon Locker",
    }
    results: list[dict[str, Any]] = []
    for carrier, keyword in carriers.items():
        resp = requests.get(
            url,
            params={
                "location": f"{lat},{lon}",
                "radius": radius_meters,
                "keyword": keyword,
                "key": api_key,
            },
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json() or {}
        for item in data.get("results") or []:
            loc = (item.get("geometry") or {}).get("location") or {}
            item_lat = loc.get("lat")
            item_lon = loc.get("lng")
            if item_lat is None or item_lon is None:
                continue
            results.append(
                {
                    "carrier": carrier,
                    "name": item.get("name"),
                    "lat": item_lat,
                    "lon": item_lon,
                    "rating": item.get("rating"),
                    "open_now": (item.get("opening_hours") or {}).get("open_now"),
                    "distance_miles": _distance_miles(lat, lon, item_lat, item_lon),
                    "place_id": item.get("place_id"),
                    "vicinity": item.get("vicinity"),
                }
            )
    return results