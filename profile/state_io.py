"""Extract/apply profile data from Streamlit session state."""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd
import streamlit as st

from .identity import get_owner_sub
from .storage import load_costs, save_costs, save_profile


def _df_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


def _records_to_df(records: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(records, columns=columns)


def _serialize_commute_profiles(commute_profiles: dict[str, Any]) -> dict[str, Any]:
    serialized = {}
    for key, profile in commute_profiles.items():
        if not isinstance(profile, dict):
            continue
        profile_copy = dict(profile)
        departure_time = profile_copy.get("departure_time")
        if hasattr(departure_time, "strftime"):
            profile_copy["departure_time"] = departure_time.strftime("%H:%M:%S")
        serialized[key] = profile_copy
    return serialized


def extract_profile() -> Dict[str, Any]:
    commute_profiles = st.session_state.get("commute_profiles", {})
    return {
        "locations": st.session_state.get("map_data", {}).get("locations", []),
        "commute": {
            "profiles": _serialize_commute_profiles(commute_profiles),
        },
        "assistant": {
            "checklist": st.session_state.get("assistant_checklist", []),
            "notes": st.session_state.get("assistant_notes", ""),
            "cost_records": st.session_state.get("assistant_cost_records", []),
            "inference_profile": st.session_state.get("assistant_inference_profile"),
        },
        "mortgage": {
            "inputs": st.session_state.get("mortgage_inputs", {}),
            "include_flags": st.session_state.get("mortgage_include_flags", {}),
            "custom_expenses_log": list(st.session_state.get("custom_expenses_log", [])),
            "take_home_log": list(st.session_state.get("take_home_log", [])),
            "cost_records": st.session_state.get("mortgage_cost_records", []),
            "inference_profile": st.session_state.get("mortgage_inference_profile"),
        },
        "hoa": {
            "cost_records": st.session_state.get("hoa_cost_records", []),
            "inference_profile": st.session_state.get("hoa_inference_profile"),
        },
    }


def apply_profile(profile: Dict[str, Any]) -> None:
    # Collapse primary sections by default when loading a profile.
    st.session_state["map_expanded"] = False
    st.session_state["commute_expanded"] = False
    st.session_state["neighborhood_expanded"] = False
    st.session_state["disaster_expanded"] = False
    st.session_state["mortgage_expanded"] = False
    st.session_state["sun_expanded"] = False
    st.session_state["zillow_expanded"] = False
    st.session_state["schools_expanded"] = False
    st.session_state["service_availability_expanded"] = False

    locations = profile.get("locations", [])
    st.session_state["map_data"] = {"locations": locations}
    st.session_state["map_badge"] = f"{len(locations)} locations"

    commute = profile.get("commute", {})
    if commute:
        profiles = commute.get("profiles", {})
        for key, commute_profile in profiles.items():
            if isinstance(commute_profile, dict):
                dep = commute_profile.get("departure_time")
                if isinstance(dep, str):
                    try:
                        commute_profile["departure_time"] = pd.to_datetime(dep).time()
                    except Exception:
                        pass
        st.session_state["commute_profiles"] = profiles

    assistant = profile.get("assistant", {})
    if assistant:
        st.session_state["assistant_checklist"] = assistant.get("checklist", [])
        st.session_state["assistant_notes"] = assistant.get("notes", "")
        st.session_state["assistant_cost_records"] = assistant.get("cost_records", [])
        st.session_state["assistant_inference_profile"] = assistant.get("inference_profile")

    mortgage = profile.get("mortgage", {})
    st.session_state["mortgage_inputs"] = mortgage.get("inputs", {})
    st.session_state["mortgage_include_flags"] = mortgage.get("include_flags", {})

    st.session_state["custom_expenses_log"] = mortgage.get("custom_expenses_log", [])
    st.session_state["take_home_log"] = mortgage.get("take_home_log", [])
    st.session_state["mortgage_cost_records"] = mortgage.get("cost_records", [])
    st.session_state["mortgage_inference_profile"] = mortgage.get("inference_profile")


    hoa = profile.get("hoa", {})
    if hoa:
        st.session_state["hoa_cost_records"] = hoa.get("cost_records", [])
        st.session_state["hoa_inference_profile"] = hoa.get("inference_profile")


def _extract_costs() -> Dict[str, Any]:
    """Extract only cost-related data from session state."""
    return {
        "assistant": {
            "cost_records": st.session_state.get("assistant_cost_records", []),
            "inference_profile": st.session_state.get("assistant_inference_profile"),
        },
        "mortgage": {
            "cost_records": st.session_state.get("mortgage_cost_records", []),
            "inference_profile": st.session_state.get("mortgage_inference_profile"),
        },
        "hoa": {
            "cost_records": st.session_state.get("hoa_cost_records", []),
            "inference_profile": st.session_state.get("hoa_inference_profile"),
        },
        "api": {
            "usage_records": st.session_state.get("api_usage_records", []),
        },
    }


def auto_save_profile() -> bool:
    """Auto-save user-level costs (not tied to a specific house). Returns True on success."""
    owner_sub = get_owner_sub()
    if not owner_sub:
        return False
    costs = _extract_costs()
    save_costs(owner_sub, costs)
    return True


def auto_load_costs() -> bool:
    """Auto-load cost records from saved user costs on page refresh. Returns True on success."""
    # Skip if costs already loaded this session
    if st.session_state.get("_costs_auto_loaded"):
        return False

    owner_sub = get_owner_sub()
    if not owner_sub:
        return False

    costs = load_costs(owner_sub)
    if not costs:
        return False

    # Load cost-related fields to session state
    assistant = costs.get("assistant", {})
    if assistant.get("cost_records"):
        st.session_state["assistant_cost_records"] = assistant.get("cost_records", [])
        st.session_state["assistant_inference_profile"] = assistant.get("inference_profile")

    mortgage = costs.get("mortgage", {})
    if mortgage.get("cost_records"):
        st.session_state["mortgage_cost_records"] = mortgage.get("cost_records", [])
        st.session_state["mortgage_inference_profile"] = mortgage.get("inference_profile")

    hoa = costs.get("hoa", {})
    if hoa.get("cost_records"):
        st.session_state["hoa_cost_records"] = hoa.get("cost_records", [])
        st.session_state["hoa_inference_profile"] = hoa.get("inference_profile")

    api = costs.get("api", {})
    if api.get("usage_records"):
        st.session_state["api_usage_records"] = api.get("usage_records", [])

    st.session_state["_costs_auto_loaded"] = True
    return True
