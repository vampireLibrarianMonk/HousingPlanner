import streamlit as st

def arm_delete(confirm_key):
    st.session_state[confirm_key] = True


def _get_loc_by_label(locations: list[dict], label: str) -> dict | None:
    for loc in locations:
        if loc["label"] == label:
            return loc
    return None
