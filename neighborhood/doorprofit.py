"""DoorProfit neighborhood data integrations."""

from __future__ import annotations

from typing import Any

from disaster.doorprofit import _get_api_key, _fetch_json
import streamlit as st


@st.cache_data(show_spinner=False, ttl=3600)
def fetch_neighborhood(address: str) -> dict[str, Any]:
    """Fetch neighborhood response for an address."""
    key = _get_api_key()
    if not key:
        raise RuntimeError(
            "Missing DOOR_PROFIT_API_KEY. Create the AWS Secrets Manager secret "
            "'houseplanner/door_profit_api_key' (preferred) or set DOOR_PROFIT_API_KEY in app/.env for local dev."
        )
    return _fetch_json(
        "https://api.doorprofit.com/v1/neighborhood",
        params={"address": address, "key": key},
    )


def get_neighborhood_payload(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize the neighborhood payload to the nested neighborhood object."""
    if not isinstance(raw, dict):
        return {}
    neighborhood = raw.get("neighborhood") or {}
    if isinstance(neighborhood, dict):
        return neighborhood
    return {}