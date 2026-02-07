"""DoorProfit hazard data integrations.

This module intentionally contains *only* request/normalization logic.
UI code (Folium layers, Streamlit tables) lives in `disaster/ui.py`.

Endpoints used:
- https://api.doorprofit.com/v1/crime?address=...&key=...
- https://api.doorprofit.com/v1/offenders?address=...&key=...
- https://api.doorprofit.com/v1/usage?key=...

We ignore /neighborhood for now.
"""

from __future__ import annotations

import os
from datetime import datetime
from functools import lru_cache
from typing import Any
import re
import requests

# Streamlit is required for the app, but allowing this module to import without it
# makes it easier to run lightweight data normalization tests (e.g., in CI).
try:
    import streamlit as st  # type: ignore
except Exception:  # pragma: no cover
    class _DummyStreamlit:
        @staticmethod
        def cache_data(**_kwargs):
            def _decorator(fn):
                return fn

            return _decorator

    st = _DummyStreamlit()  # type: ignore

DOOR_PROFIT_KEY_ENV = "DOOR_PROFIT_API_KEY"
DOOR_PROFIT_SECRET_NAME = "houseplanner/door_profit_api_key"


@lru_cache(maxsize=1)
def _get_secret(secret_name: str) -> str:
    """Read a plaintext secret from AWS Secrets Manager."""
    # Lazy import so the rest of the module (e.g., dedupe helpers) can be used
    # in environments that don't have AWS libs installed.
    try:
        import boto3  # type: ignore
        from botocore.exceptions import ClientError  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "boto3/botocore are required to load secrets from AWS Secrets Manager. "
            "Install them or set DOOR_PROFIT_API_KEY in the environment."
        ) from exc

    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as exc:
        raise RuntimeError(f"Unable to load secret '{secret_name}': {exc}")
    return resp["SecretString"]


def _get_api_key() -> str:
    """Return DoorProfit key using the project convention.

    **Order of precedence (per your constraint: no secrets.toml):**
    1) AWS Secrets Manager secret: `houseplanner/door_profit_api_key`
    2) Environment variable: `DOOR_PROFIT_API_KEY` (local dev fallback)
    """

    # 1) AWS Secrets Manager (preferred)
    try:
        sm_val = (_get_secret(DOOR_PROFIT_SECRET_NAME) or "").strip()
        if sm_val:
            return sm_val
    except Exception:
        pass

    # 2) Environment fallback
    return (os.getenv(DOOR_PROFIT_KEY_ENV) or "").strip()


def _fetch_json(url: str, *, params: dict[str, Any]) -> dict[str, Any]:
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"DoorProfit returned non-object JSON: {type(data)}")
    if data.get("success") is False:
        # DoorProfit seems to use {success:false, error:...} style failures.
        raise RuntimeError(f"DoorProfit error response: {data}")
    return data


def _norm_text(value: Any) -> str:
    """Normalize text for dedupe keys."""
    if value is None:
        return ""
    s = str(value).strip().lower()
    # Collapse whitespace and normalize common punctuation differences.
    s = re.sub(r"\s+", " ", s)
    return s


def _round_coord(value: Any, decimals: int = 5) -> float | None:
    try:
        return round(float(value), decimals)
    except (TypeError, ValueError):
        return None


def _dedupe_list(items: list[dict[str, Any]], key_fn) -> list[dict[str, Any]]:
    """Order-preserving dedupe for list-of-dicts payloads."""
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        try:
            k = key_fn(item)
        except Exception:
            # If keying fails for a record, keep it (avoid data loss).
            out.append(item)
            continue
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def _crime_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _norm_text(item.get("type")),
        _norm_text(item.get("date")),
        _norm_text(item.get("address")),
        _round_coord(item.get("lat")),
        _round_coord(item.get("lng")),
    )


def _offender_key(item: dict[str, Any]) -> tuple[Any, ...]:
    # Prefer a stable unique identifier if present.
    src = _norm_text(item.get("source_url"))
    if src:
        return ("source_url", src)
    return (
        "fallback",
        _norm_text(item.get("name")),
        _norm_text(item.get("dob")),
        _norm_text(item.get("address")),
        _round_coord(item.get("lat")),
        _round_coord(item.get("lng")),
    )


def dedupe_crime_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the payload with incident duplicates removed."""
    incidents = (payload or {}).get("incidents")
    if not isinstance(incidents, dict):
        return payload
    data = incidents.get("data")
    if not isinstance(data, list) or not data:
        return payload
    deduped = _dedupe_list(data, _crime_key)
    if len(deduped) == len(data):
        return payload

    out = dict(payload)
    out_inc = dict(incidents)
    out_inc["data"] = deduped
    # Many APIs have count, keep it consistent if present.
    if "count" in out_inc:
        out_inc["count"] = len(deduped)
    out["incidents"] = out_inc
    return out


def dedupe_offenders_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the payload with offender duplicates removed."""
    offenders = (payload or {}).get("offenders")
    if not isinstance(offenders, list) or not offenders:
        return payload
    deduped = _dedupe_list(offenders, _offender_key)
    if len(deduped) == len(offenders):
        return payload

    out = dict(payload)
    out["offenders"] = deduped
    # Keep common count fields consistent when present.
    for k in ("offenders_count", "total_count"):
        if k in out:
            out[k] = len(deduped)
    return out


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_crime(address: str) -> dict[str, Any]:
    """Fetch crime response for an address."""
    key = _get_api_key()
    if not key:
        raise RuntimeError(
            "Missing DOOR_PROFIT_API_KEY. Create the AWS Secrets Manager secret "
            "'houseplanner/door_profit_api_key' (preferred) or set DOOR_PROFIT_API_KEY in app/.env for local dev."
        )
    data = _fetch_json(
        "https://api.doorprofit.com/v1/crime",
        params={"address": address, "key": key},
    )
    return dedupe_crime_response(data)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_offenders(address: str) -> dict[str, Any]:
    """Fetch sex offender response for an address."""
    key = _get_api_key()
    if not key:
        raise RuntimeError(
            "Missing DOOR_PROFIT_API_KEY. Create the AWS Secrets Manager secret "
            "'houseplanner/door_profit_api_key' (preferred) or set DOOR_PROFIT_API_KEY in app/.env for local dev."
        )
    data = _fetch_json(
        "https://api.doorprofit.com/v1/offenders",
        params={"address": address, "key": key},
    )
    return dedupe_offenders_response(data)


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_usage() -> dict[str, Any]:
    """Fetch DoorProfit API usage and plan information."""
    key = _get_api_key()
    if not key:
        raise RuntimeError(
            "Missing DOOR_PROFIT_API_KEY. Create the AWS Secrets Manager secret "
            "'houseplanner/door_profit_api_key' (preferred) or set DOOR_PROFIT_API_KEY in app/.env for local dev."
        )
    return _fetch_json(
        "https://api.doorprofit.com/v1/usage",
        params={"key": key},
    )


def crime_incidents_to_features(crime_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert DoorProfit crime incidents payload to GeoJSON-like point features."""
    crime_json = dedupe_crime_response(crime_json or {})
    incidents = (((crime_json or {}).get("incidents") or {}).get("data") or [])
    features: list[dict[str, Any]] = []
    for item in incidents:
        lat = item.get("lat")
        lng = item.get("lng")
        if lat is None or lng is None:
            continue
        props = dict(item)
        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Point", "coordinates": [float(lng), float(lat)]},
            }
        )
    return features


def offenders_to_features(offenders_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert DoorProfit offenders payload to GeoJSON-like point features."""
    offenders_json = dedupe_offenders_response(offenders_json or {})
    offenders = (offenders_json or {}).get("offenders") or []
    features: list[dict[str, Any]] = []
    for item in offenders:
        lat = item.get("lat")
        lng = item.get("lng")
        if lat is None or lng is None:
            continue
        props = dict(item)
        features.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Point", "coordinates": [float(lng), float(lat)]},
            }
        )
    return features


def parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        return None
