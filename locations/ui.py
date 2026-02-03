import streamlit as st

from .providers import geocode_once
from .logic import arm_delete
from profile.ui import save_current_profile

def render_locations():
    with st.expander(
        f"Location Management  •  {st.session_state['map_badge']}",
        expanded=st.session_state["map_expanded"]
    ):
        st.subheader("Add a Location")

        # -----------------------------
        # Add-location form (keeps UI stable)
        # -----------------------------
        with st.form("add_location_form", clear_on_submit=True):
            cols = st.columns([0.25, 0.55, 0.2])

            with cols[0]:
                location_label = st.text_input(
                    "Location Label",
                    placeholder="House, Work, Daycare",
                    label_visibility="collapsed"
                )

            with cols[1]:
                location_address = st.text_input(
                    "Location Address",
                    placeholder="Street, City, State",
                    label_visibility="collapsed"
                )

            with cols[2]:
                submitted = st.form_submit_button("Add")

        # -----------------------------
        # Handle submission
        # -----------------------------
        if submitted:
            if not location_label or not location_address:
                st.warning("Please enter both a label and an address.")
            else:
                try:
                    lat, lon = geocode_once(location_address)

                    normalized_label = location_label.strip()
                    updated = False
                    updated_house = False
                    for loc in st.session_state["map_data"]["locations"]:
                        if loc.get("label", "").strip().lower() == normalized_label.lower():
                            loc.update({
                                "label": normalized_label,
                                "address": location_address,
                                "lat": lat,
                                "lon": lon,
                            })
                            updated = True
                            updated_house = normalized_label.lower() == "house"
                            break

                    if not updated:
                        st.session_state["map_data"]["locations"].append({
                            "label": normalized_label,
                            "address": location_address,
                            "lat": lat,
                            "lon": lon,
                        })
                        updated_house = normalized_label.lower() == "house"

                    # Update badge and keep section open
                    count = len(st.session_state["map_data"]["locations"])
                    st.session_state["map_badge"] = f"{count} locations"

                    # KEEP LOCATION SECTION OPEN
                    st.session_state["map_expanded"] = True

                    if updated_house:
                        st.rerun()

                except Exception as e:
                    st.error(f"Geocoding error: {e}")

        # -----------------------------
        # Build and render map + table
        # -----------------------------
        locations = st.session_state["map_data"]["locations"]

        table_col = st.columns([1])[0]

        with table_col:
            st.subheader("Locations")

            if not locations:
                st.caption("No locations added yet.")
            else:
                header_cols = st.columns([0.25, 0.55, 0.2])
                with header_cols[0]:
                    st.markdown("**Label**")
                with header_cols[1]:
                    st.markdown("**Address**")
                with header_cols[2]:
                    st.markdown("**Delete**")

                st.divider()

                for idx, loc in enumerate(locations):
                    row_cols = st.columns([0.25, 0.55, 0.2])

                    with row_cols[0]:
                        st.write(loc["label"])

                    with row_cols[1]:
                        st.caption(loc["address"])

                    with row_cols[2]:
                        confirm_key = f"confirm_delete_{idx}"

                        if confirm_key not in st.session_state:
                            st.session_state[confirm_key] = False

                        if not st.session_state[confirm_key]:
                            st.button(
                                "Delete",
                                key=f"delete_{idx}",
                                type="secondary",
                                on_click=arm_delete,
                                args=(confirm_key,)
                            )
                        else:
                            if st.button(
                                    "Confirm",
                                    key=f"confirm_{idx}",
                                    type="primary"
                            ):
                                st.session_state["map_data"]["locations"].pop(idx)
                                st.session_state.pop(confirm_key, None)

                                count = len(st.session_state["map_data"]["locations"])
                                st.session_state["map_badge"] = f"{count} locations"

                                # KEEP LOCATION SECTION OPEN AFTER DELETE
                                st.session_state["map_expanded"] = True

                                st.rerun()

            if st.button("Save", key="save_locations_profile"):
                try:
                    save_path = save_current_profile()
                    st.success(f"Saved to {save_path}")
                except Exception as exc:
                    st.error(f"Save failed: {exc}")