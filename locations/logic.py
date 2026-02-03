import streamlit as st

def arm_delete(confirm_key):
    st.session_state[confirm_key] = True


def _get_loc_by_label(locations: list[dict], label: str) -> dict | None:
    target = label.strip().lower()
    for loc in locations:
        if loc.get("label", "").strip().lower() == target:
            return loc
    return None
