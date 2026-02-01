"""Extract/apply profile data from Streamlit session state."""

from __future__ import annotations

from typing import Any, Dict

import pandas as pd
import streamlit as st


def _df_to_records(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    return df.to_dict(orient="records")


def _records_to_df(records: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame(columns=columns)
    return pd.DataFrame(records, columns=columns)


def extract_profile() -> Dict[str, Any]:
    return {
        "locations": st.session_state.get("map_data", {}).get("locations", []),
        "assistant": {
            "checklist": st.session_state.get("assistant_checklist", []),
            "notes": st.session_state.get("assistant_notes", ""),
            "cost_records": st.session_state.get("assistant_cost_records", []),
            "inference_profile": st.session_state.get("assistant_inference_profile"),
        },
        "mortgage": {
            "inputs": st.session_state.get("mortgage_inputs", {}),
            "include_flags": st.session_state.get("mortgage_include_flags", {}),
            "custom_expenses": _df_to_records(
                st.session_state.get("custom_expenses_df", pd.DataFrame())
            ),
            "take_home_sources": _df_to_records(
                st.session_state.get("take_home_sources_df", pd.DataFrame())
            ),
        },
    }


def apply_profile(profile: Dict[str, Any]) -> None:
    locations = profile.get("locations", [])
    st.session_state["map_data"] = {"locations": locations}
    st.session_state["map_badge"] = f"{len(locations)} locations"
    st.session_state["map_expanded"] = True

    assistant = profile.get("assistant", {})
    if assistant:
        st.session_state["assistant_checklist"] = assistant.get("checklist", [])
        st.session_state["assistant_notes"] = assistant.get("notes", "")
        st.session_state["assistant_cost_records"] = assistant.get("cost_records", [])
        st.session_state["assistant_inference_profile"] = assistant.get("inference_profile")

    mortgage = profile.get("mortgage", {})
    st.session_state["mortgage_inputs"] = mortgage.get("inputs", {})
    st.session_state["mortgage_include_flags"] = mortgage.get("include_flags", {})

    st.session_state["custom_expenses_df"] = _records_to_df(
        mortgage.get("custom_expenses", []),
        ["Label", "Amount", "Cadence"],
    )
    st.session_state["take_home_sources_df"] = _records_to_df(
        mortgage.get("take_home_sources", []),
        ["Source", "Amount", "Cadence"],
    )

    st.session_state["mortgage_expanded"] = True