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