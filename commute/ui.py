import os
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

from locations.logic import _get_loc_by_label
from .logic import compute_commute


def render_commute():
    with st.expander(
        "Commute Analysis",
        expanded=st.session_state["commute_expanded"]
    ):
        st.subheader("Trip Order (returns to Home of Record)")

        # ---------------------------------------------
        # Traffic & Routing Assumptions
        # ---------------------------------------------
        with st.expander("Traffic & Routing Assumptions", expanded=False):
            st.markdown(r"""
### Traffic Modeling (Current)

This commute analysis uses **OpenRouteService – driving-car** routing.

**What is modeled**
- Average, non-time-specific driving speeds
- Standard road hierarchy and turn costs
- Deterministic routing (same inputs → same outputs)

**What is NOT modeled**
- Live traffic conditions
- Rush-hour congestion
- Day-of-week or time-of-day variation
- Incidents, construction, or weather impacts

**Implications**
- Results represent a **baseline / typical commute**
- Suitable for comparing route orderings and budgeting
- Not suitable for predicting peak-hour delays

**Upgrade Path**
This section can be upgraded to a traffic-aware provider
(e.g., Google Distance Matrix, TomTom, HERE) without
changing the Trip Order UI or data model.
""")

        locations = st.session_state.get("map_data", {}).get("locations", [])
        if not locations:
            st.info("Add locations in the Map section first (House, Work, Daycare, etc.).")
            return

        # ---------------------------------------------
        # Routing Method Selection
        # ---------------------------------------------
        routing_method = st.selectbox(
            "Routing Method",
            ["OpenRouteService (average traffic)", "Google (traffic-aware)"],
            help="Choose between average traffic (ORS) or traffic-aware routing (Google)."
        )

        # ---------------------------------------------
        # Routing API Keys
        # ---------------------------------------------
        ors_api_key = os.getenv("ORS_API_KEY")
        google_api_key = os.getenv("GOOGLE_MAPS_API_KEY")

        if routing_method.startswith("OpenRouteService"):
            if not ors_api_key:
                st.error("ORS_API_KEY is not set.")
                return
        else:
            if not google_api_key:
                st.error("GOOGLE_MAPS_API_KEY is not set.")
                return

        # ---------------------------------------------
        # Departure Time
        # ---------------------------------------------
        departure_time = st.time_input(
            "Departure Time (from Home)",
            value=pd.to_datetime("07:45").time(),
            help="Used for traffic-aware routing (Google only)."
        )

        # ---------------------------------------------
        # Home of Record
        # ---------------------------------------------
        labels = [l["label"] for l in locations]
        home_label = st.selectbox(
            "Home of Record (start/end)",
            options=labels,
            index=labels.index("House") if "House" in labels else 0
        )

        home = _get_loc_by_label(locations, home_label)
        if not home:
            st.error("Home of record not found.")
            return

        # ---------------------------------------------
        # Commute Table Initialization
        # ---------------------------------------------
        if "commute_table" not in st.session_state:
            rows = []
            for loc in locations:
                if loc["label"] == home_label:
                    continue
                rows.append({
                    "Include": False,
                    "Revisit": False,
                    "Order": 1,
                    "Loiter (min)": 0,
                    "Label": loc["label"],
                    "Address": loc["address"],
                })
            st.session_state["commute_table"] = pd.DataFrame(rows)

        if "commute_table_synced" not in st.session_state:
            existing_labels = set(st.session_state["commute_table"]["Label"])

            new_rows = []
            for loc in locations:
                if loc["label"] == home_label:
                    continue
                if loc["label"] not in existing_labels:
                    new_rows.append({
                        "Include": False,
                        "Revisit": False,
                        "Order": 1,
                        "Loiter (min)": 0,
                        "Label": loc["label"],
                        "Address": loc["address"],
                    })

            if new_rows:
                st.session_state["commute_table"] = pd.concat(
                    [st.session_state["commute_table"], pd.DataFrame(new_rows)],
                    ignore_index=True
                )

            st.session_state["commute_table_synced"] = True

        edited = st.data_editor(
            st.session_state["commute_table"],
            width="stretch",
            hide_index=True,
            column_config={
                "Include": st.column_config.CheckboxColumn("Include"),
                "Revisit": st.column_config.CheckboxColumn("Revisit"),
                "Order": st.column_config.NumberColumn("Order", min_value=1, step=1),
                "Loiter (min)": st.column_config.NumberColumn("Loiter (min)", min_value=0, step=5),
                "Label": st.column_config.TextColumn("Label", disabled=True),
                "Address": st.column_config.TextColumn("Address", disabled=True),
            },
        )

        # ---------------------------------------------
        # Build itinerary
        # ---------------------------------------------
        stops_df = edited[edited["Include"] == True].copy()
        if stops_df.empty:
            st.info("Select at least one stop.")
            can_compute = False
        else:
            can_compute = True

        stops_df.sort_values(["Order", "Label"], inplace=True)

        compute = st.button("Compute Commute", type="primary")

        if compute and can_compute:
            try:
                result = compute_commute(
                    locations=locations,
                    home=home,
                    stops_df=stops_df,
                    routing_method=routing_method,
                    ors_api_key=ors_api_key,
                    google_api_key=google_api_key,
                    departure_time=departure_time,
                )
            except Exception as e:
                st.error(str(e))
                return

            provider_key = (
                "ORS"
                if routing_method.startswith("OpenRouteService")
                else "Google"
            )

            st.session_state["commute_results"][provider_key] = {
                "segments": pd.DataFrame(result["segments"]),
                "total_m": result["total_m"],
                "total_s": result["total_s"],
                "segment_routes": result["segment_routes"],
            }

            st.session_state["last_commute_provider"] = provider_key
            st.session_state["pending_layer_defaults"] = provider_key
            st.rerun()

        # ---------------------------------------------
        # Results + Map (UNCHANGED)
        # ---------------------------------------------
        if st.session_state.get("commute_results"):
            provider_key = st.session_state.get("last_commute_provider")
            res = st.session_state["commute_results"].get(provider_key)

            if res:
                st.subheader(f"{provider_key} Commute Results")
                st.dataframe(res["segments"], width="stretch", hide_index=True)

                st.metric("Total Distance", f"{res['total_m'] / 1609.344:,.2f} mi")
                st.metric("Total Drive Time", f"{res['total_s'] / 60.0:,.1f} min")

        if st.session_state.get("last_commute_provider"):
            st.subheader("Commute Map")

            pending = st.session_state.pop("pending_layer_defaults", None)
            if pending == "ORS":
                st.session_state["show_ors"] = True
                st.session_state["show_google"] = False
                st.session_state["show_markers"] = True
            elif pending == "Google":
                st.session_state["show_google"] = True
                st.session_state["show_ors"] = False
                st.session_state["show_markers"] = True

            show_ors = st.checkbox("Show ORS routes", key="show_ors")
            show_google = st.checkbox("Show Google routes", key="show_google")
            show_markers = st.checkbox("Show depart / arrive markers", key="show_markers")

            m = folium.Map(
                location=[39.8283, -98.5795],
                zoom_start=4,
                tiles="OpenStreetMap"
            )

            bounds = []
            for loc in locations:
                bounds.append([loc["lat"], loc["lon"]])
                is_house = loc["label"].strip().lower() == "house"

                folium.Marker(
                    location=[loc["lat"], loc["lon"]],
                    popup=f"<b>{loc['label']}</b><br>{loc['address']}",
                    icon=folium.Icon(
                        color="green" if is_house else "blue",
                        icon="home" if is_house else "info-sign"
                    ),
                ).add_to(m)

            if bounds:
                m.fit_bounds(bounds)

            for provider, res in st.session_state["commute_results"].items():
                for seg in res.get("segment_routes", []):
                    pts = seg["points"]
                    if not pts:
                        continue

                    show_route = (
                        (provider == "ORS" and show_ors)
                        or (provider == "Google" and show_google)
                    )

                    if show_route:
                        folium.PolyLine(
                            locations=pts,
                            color="#5E35B1" if provider == "ORS" else "#00695C",
                            weight=5,
                            opacity=0.85,
                            tooltip=f"{provider}: {seg['from']} → {seg['to']}",
                        ).add_to(m)

                    if show_markers:
                        folium.CircleMarker(
                            location=pts[0],
                            radius=6,
                            color="#1565C0",
                            fill=True,
                            fill_color="#1565C0",
                        ).add_to(m)

                        folium.CircleMarker(
                            location=pts[-1],
                            radius=6,
                            color="#C62828",
                            fill=True,
                            fill_color="#C62828",
                        ).add_to(m)

            st_folium(m, width=900, height=500)
