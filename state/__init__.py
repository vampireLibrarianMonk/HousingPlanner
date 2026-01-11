import streamlit as st
import pandas as pd
import time

from locations.providers import geocode_once

def init_state():
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

    if "mortgage_expanded" not in st.session_state:
        st.session_state["mortgage_expanded"] = False

    if "mortgage_badge" not in st.session_state:
        st.session_state["mortgage_badge"] = "Monthly: —"

    if "custom_expenses" not in st.session_state:
        st.session_state["custom_expenses"] = pd.DataFrame(
            columns=["Label", "Amount", "Cadence"]
        )
