import time

import pandas as pd
import streamlit as st
from locations.providers import geocode_once
from profile.state_io import auto_load_costs


def init_state():
    # Auto-load saved costs on page refresh
    auto_load_costs()

    # -----------------------------
    # Session State
    # -----------------------------
    if "commute_results" not in st.session_state:
        st.session_state["commute_results"] = {}

    if "commute_expanded" not in st.session_state:
        st.session_state["commute_expanded"] = False

    if "neighborhood_expanded" not in st.session_state:
        st.session_state["neighborhood_expanded"] = False

    if "custom_expenses" not in st.session_state:
        st.session_state["custom_expenses"] = pd.DataFrame(
            columns=["Label", "Amount", "Cadence"]
        )

    if "disaster_expanded" not in st.session_state:
        st.session_state["disaster_expanded"] = False

    if "disaster_radius_miles" not in st.session_state:
        st.session_state["disaster_radius_miles"] = 5

    if "hz_disaster_history" not in st.session_state:
        st.session_state["hz_disaster_history"] = False

    if "hz_earthquake" not in st.session_state:
        st.session_state["hz_earthquake"] = False

    if "hz_flood" not in st.session_state:
        st.session_state["hz_flood"] = False

    if "hz_heat" not in st.session_state:
        st.session_state["hz_heat"] = False

    if "hz_land_use" not in st.session_state:
        st.session_state["hz_land_use"] = False

    if "hz_wildfire" not in st.session_state:
        st.session_state["hz_wildfire"] = False

    if "hz_wind" not in st.session_state:
        st.session_state["hz_wind"] = False

    if "hz_crime" not in st.session_state:
        st.session_state["hz_crime"] = False

    if "hz_sex_offenders" not in st.session_state:
        st.session_state["hz_sex_offenders"] = False

    if "map_badge" not in st.session_state:
        st.session_state["map_badge"] = "3 locations"

    if "map_data" not in st.session_state:
        default_locations = [
            {
                "label": "House",
                "address": "4005 Ancient Oak Ct, Annandale, VA 22003",
            },
            # {
            #     "label": "House",
            #     "address": "6246 Skyway, Paradise, CA 95969",
            # },
            {
                "label": "Work",
                "address": "7500 GEOINT Dr, Springfield, VA 22150",
            },
            {
                "label": "Daycare",
                "address": "6935 Columbia Pike, Annandale, VA 22003",
            },
        ]

        locations = []
        for i, loc in enumerate(default_locations):
            lat, lon = geocode_once(loc["address"])
            locations.append({
                "label": loc["label"],
                "address": loc["address"],
                "lat": lat,
                "lon": lon,
            })

            # Be polite to Nominatim (1 request / second)
            if i < len(default_locations) - 1:
                time.sleep(1)

        st.session_state["map_data"] = {
            "locations": locations
        }

        st.session_state["map_badge"] = f"{len(locations)} locations"

    if "map_expanded" not in st.session_state:
        st.session_state["map_expanded"] = False

    if "mortgage_badge" not in st.session_state:
        st.session_state["mortgage_badge"] = "Monthly: —"

    if "mortgage_expanded" not in st.session_state:
        st.session_state["mortgage_expanded"] = False

    if "show_google" not in st.session_state:
        st.session_state["show_google"] = False

    if "show_markers" not in st.session_state:
        st.session_state["show_markers"] = False

    if "show_ors" not in st.session_state:
        st.session_state["show_ors"] = False

    if "sun_expanded" not in st.session_state:
        st.session_state["sun_expanded"] = False