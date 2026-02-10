from datetime import datetime

import boto3
from botocore.exceptions import ClientError

import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

from locations.logic import _get_loc_by_label
from profile.ui import save_current_profile
from .logic import compute_commute, compute_infrastructure_support

@st.cache_data(show_spinner=False)
def _get_secret(secret_name: str) -> str:
    client = boto3.client("secretsmanager")
    try:
        resp = client.get_secret_value(SecretId=secret_name)
    except ClientError as e:
        raise RuntimeError(f"Unable to load secret '{secret_name}': {e}")
    return resp["SecretString"]


def _init_commute_profile(name: str, locations: list[dict]) -> dict:
    rows = []
    home_label = "House"
    order_counter = 1
    for loc in locations:
        if loc["label"] == home_label:
            continue
        rows.append({
            "Include": False,
            "Revisit": False,
            "Order": order_counter,
            "Loiter (min)": 0,
            "Label": loc["label"],
            "Address": loc["address"],
        })
        order_counter += 1
    return {
        "name": name,
        "routing_method": "OpenRouteService (average traffic)",
        "departure_time": pd.to_datetime("07:45").time(),
        "home_label": home_label,
        "commute_table": rows,
        "commute_results": {},
        "last_commute_provider": None,
        "infra": {},
    }


def _sync_commute_table(profile: dict, locations: list[dict]) -> pd.DataFrame:
    table_records = profile.get("commute_table", [])
    table_df = pd.DataFrame(table_records)

    if table_df.empty:
        return table_df

    existing_labels = set(table_df.get("Label", []))
    new_rows = []
    next_order = 1
    if not table_df.empty and "Order" in table_df.columns:
        numeric_orders = pd.to_numeric(table_df["Order"], errors="coerce")
        if numeric_orders.notna().any():
            next_order = int(numeric_orders.max()) + 1
    for loc in locations:
        if loc["label"] == profile.get("home_label"):
            continue
        if loc["label"] not in existing_labels:
            new_rows.append({
                "Include": False,
                "Revisit": False,
                "Order": next_order,
                "Loiter (min)": 0,
                "Label": loc["label"],
                "Address": loc["address"],
            })
            next_order += 1

    if new_rows:
        table_df = pd.concat([table_df, pd.DataFrame(new_rows)], ignore_index=True)

    return table_df


def _render_commute_tab(profile_key: str, profile: dict, locations: list[dict]):
    st.subheader("Trip Order (returns to Home of Record)")
    st.markdown(
        """
        <div style="display:flex;align-items:center;gap:12px;margin-top:6px;">
            <div style="display:flex;align-items:center;gap:6px;">
                <span style="display:inline-block;width:14px;height:14px;"
                    "background:#FFFFFF;border:1px solid #000;"></span>
                <span style="font-size:12px;">Outbound legs</span>
            </div>
            <div style="display:flex;align-items:center;gap:6px;">
                <span style="display:inline-block;width:14px;height:14px;"
                    "background:#000000;border:1px solid #000;"></span>
                <span style="font-size:12px;">Return to Home</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ---------------------------------------------
    # Traffic & Routing Assumptions
    # ---------------------------------------------
    with st.expander("Traffic & Routing Assumptions", expanded=False):
        st.markdown(r"""
### Routing Engines (Current)

This commute analysis supports three providers:
- **OpenRouteService (ORS)** – average traffic baseline, deterministic routing
- **Google Routes** – traffic-aware (uses departure time)
- **Waze** – supplemental routing + incident overlays

**What is modeled**
- Average driving speeds (ORS), or traffic-aware times (Google)
- Standard road hierarchy and turn costs
- Deterministic routing (same inputs → same outputs)

**What is NOT modeled**
- Weather impacts or road closures
- Time-of-day variation for ORS/Waze
- Live traffic incidents unless explicitly shown in overlays

### Hazards & Infrastructure Overlay
- **Incidents** are shown as red points (Waze alerts)
- **Traffic jams** are shown as orange lines
- Overlays **do not alter routing**; they are visual context only

### Map Legend & Markers
- Each location type (Home, Work, Daycare, etc.) has a unique icon + color
- Route colors represent leg order (first stop = first color, etc.)
- The legend is **dynamic** and only includes location types present in your trip

### Service Info & Limitations
- API keys are loaded from AWS Secrets Manager
- Waze may not return geometry, so ORS geometry is used as fallback for display
- Results are cached for performance; re-run for updated data

**Upgrade Path**
This section can be expanded with additional traffic-aware providers
(TomTom, HERE, Google Distance Matrix) without changing the Trip Order UI.
""")

    if not locations:
        st.info("Add locations in the Map section first (House, Work, Daycare, etc.).")
        return

    # ---------------------------------------------
    # Routing Settings (form to avoid reruns)
    # ---------------------------------------------
    labels = [l["label"] for l in locations]
    default_home = profile.get("home_label") or ("House" if "House" in labels else labels[0])
    routing_options = [
        "OpenRouteService (average traffic)",
        "Google (traffic-aware)",
        "Waze (supplemental)",
    ]
    current_routing = profile.get("routing_method", routing_options[0])
    routing_index = routing_options.index(current_routing) if current_routing in routing_options else 0

    with st.form(f"routing_settings_form_{profile_key}"):
        routing_method = st.selectbox(
            "Routing Method",
            routing_options,
            help=(
                "Choose between average traffic (ORS), traffic-aware (Google), "
                "or supplemental Waze routing. Note: Waze routing may not always "
                "include route geometry for map display (we fall back to ORS geometry)."
            ),
            key=f"routing_method_input_{profile_key}",
            index=routing_index,
        )
        departure_time = st.time_input(
            "Departure Time (from Home)",
            value=profile.get("departure_time") or pd.to_datetime("07:45").time(),
            help="Used for traffic-aware routing (Google only).",
            key=f"departure_time_input_{profile_key}",
        )
        home_label = st.selectbox(
            "Home of Record (start/end)",
            options=labels,
            index=labels.index(default_home) if default_home in labels else 0,
            key=f"home_label_input_{profile_key}",
        )

        compute = st.form_submit_button("Compute Commute", type="primary")

    if compute:
        profile["routing_method"] = routing_method
        profile["departure_time"] = departure_time
        profile["home_label"] = home_label
        st.session_state["commute_profiles"][profile_key] = profile

    # Use persisted settings for downstream logic
    routing_method = profile.get("routing_method", routing_options[0])
    departure_time = profile.get("departure_time") or pd.to_datetime("07:45").time()
    home_label = profile.get("home_label") or default_home

    home = _get_loc_by_label(locations, home_label)
    if not home:
        st.error("Home of record not found.")
        return


    # ---------------------------------------------
    # Commute Table Initialization
    # ---------------------------------------------
    table_state_key = f"commute_table_state_{profile_key}"
    editor_key = f"commute_table_editor_{profile_key}"
    locations_key = f"commute_locations_key_{profile_key}"

    location_signature = tuple((loc["label"], loc["address"]) for loc in locations)
    if st.session_state.get(locations_key) != location_signature:
        st.session_state[table_state_key] = _sync_commute_table(profile, locations)
        st.session_state[locations_key] = location_signature

    edited = st.data_editor(
        st.session_state[table_state_key],
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
        key=editor_key,
    )

    edited_state = st.session_state.get(editor_key, edited)
    if isinstance(edited_state, pd.DataFrame):
        profile["commute_table"] = edited_state.to_dict(orient="records")
        st.session_state[table_state_key] = edited_state
        st.session_state["commute_profiles"][profile_key] = profile

    # ---------------------------------------------
    # Build itinerary
    # ---------------------------------------------
    table_for_compute = edited_state if isinstance(edited_state, pd.DataFrame) else edited
    if isinstance(table_for_compute, pd.DataFrame):
        table_for_compute = table_for_compute.copy()
        if "Include" in table_for_compute.columns:
            table_for_compute["Include"] = table_for_compute["Include"].astype(bool)
        if "Order" in table_for_compute.columns:
            table_for_compute["Order"] = pd.to_numeric(
                table_for_compute["Order"],
                errors="coerce",
            )
        stops_df = table_for_compute[table_for_compute["Include"] == True].copy()
    else:
        stops_df = pd.DataFrame()
    if stops_df.empty:
        if "House" not in labels:
            st.warning("Home of Record is missing. Add a House location first.")
        st.info("Select at least one stop.")
        can_compute = False
    else:
        can_compute = True

    stops_df.sort_values(["Order", "Label"], inplace=True)
    duplicate_orders = stops_df["Order"].duplicated().any()
    if stops_df["Order"].isna().any():
        st.error("Each included stop must have an Order value.")
        can_compute = False
    if duplicate_orders:
        st.error("Each included stop must have a unique Order value.")
        can_compute = False

    status_key = f"commute_status_{profile_key}"
    progress_key = f"commute_progress_{profile_key}"
    progress_label_key = f"commute_progress_label_{profile_key}"

    ors_api_key = None
    google_api_key = None
    waze_api_key = None

    if status_key not in st.session_state:
        st.session_state[status_key] = ""
    if progress_key not in st.session_state:
        st.session_state[progress_key] = 0
    if progress_label_key not in st.session_state:
        st.session_state[progress_label_key] = "Idle"

    if not can_compute:
        st.session_state[status_key] = "Update stops and unique Order values to compute."
        st.session_state[progress_key] = 0
        st.session_state[progress_label_key] = "Awaiting valid input"

    button_col, status_col = st.columns([0.2, 0.8])
    with button_col:
        st.caption("Use the form above to compute.")
    with status_col:
        status_placeholder = st.empty()
        status_progress = st.empty()
        status_placeholder.caption(st.session_state[status_key])
        status_progress.progress(
            st.session_state[progress_key],
            text=st.session_state[progress_label_key],
        )

    if compute and can_compute:
        # ---------------------------------------------
        # Routing API Keys (AWS Secrets Manager)
        # ---------------------------------------------
        try:
            ors_api_key = _get_secret("houseplanner/ors_api_key")
            google_api_key = _get_secret("houseplanner/google_maps_api_key")
            waze_api_key = _get_secret("houseplanner/waze_api_key")
        except Exception as e:
            st.error(str(e))
            return

        if routing_method.startswith("OpenRouteService"):
            if not ors_api_key:
                st.error("ORS API key could not be loaded.")
                return
        elif routing_method.startswith("Google"):
            if not google_api_key:
                st.error("Google Maps API key could not be loaded.")
                return
        else:
            if not waze_api_key:
                st.error("Waze API key could not be loaded.")
                return

        st.session_state[status_key] = "Preparing commute request..."
        st.session_state[progress_key] = 5
        st.session_state[progress_label_key] = "Validating itinerary"
        status_placeholder.caption(st.session_state[status_key])
        status_progress.progress(
            st.session_state[progress_key],
            text=st.session_state[progress_label_key],
        )
        try:
            st.session_state[progress_key] = 15
            st.session_state[progress_label_key] = "Requesting routing engine"
            status_progress.progress(
                st.session_state[progress_key],
                text=st.session_state[progress_label_key],
            )
            def _update_status(message: str):
                st.session_state[status_key] = message
                status_placeholder.caption(message)

            result = compute_commute(
                locations=locations,
                home=home,
                stops_df=stops_df,
                routing_method=routing_method,
                ors_api_key=ors_api_key,
                google_api_key=google_api_key,
                waze_api_key=waze_api_key,
                departure_time=departure_time,
                status_callback=_update_status,
            )
            st.session_state[progress_key] = 55
            st.session_state[progress_label_key] = "Normalizing route geometry"
            status_progress.progress(
                st.session_state[progress_key],
                text=st.session_state[progress_label_key],
            )
            st.session_state[progress_key] = 70
            st.session_state[progress_label_key] = "Fetching infrastructure events"
            status_progress.progress(
                st.session_state[progress_key],
                text=st.session_state[progress_label_key],
            )
            infra = compute_infrastructure_support(
                route_points=result.get("route_points", []),
                waze_api_key=waze_api_key,
            )
            infra["fetched_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            st.session_state[progress_key] = 90
            st.session_state[progress_label_key] = "Building summary + map"
            status_progress.progress(
                st.session_state[progress_key],
                text=st.session_state[progress_label_key],
            )
        except Exception as e:
            st.session_state[status_key] = "Compute failed. Check input settings."
            st.session_state[progress_key] = 0
            st.session_state[progress_label_key] = "Error"
            status_placeholder.caption(st.session_state[status_key])
            status_progress.progress(
                st.session_state[progress_key],
                text=st.session_state[progress_label_key],
            )
            st.error(str(e))
            return

        if routing_method.startswith("OpenRouteService"):
            provider_key = "ORS"
        elif routing_method.startswith("Google"):
            provider_key = "Google"
        else:
            provider_key = "Waze"

        # Store results under provider key (preserves other providers' results)
        if "commute_results" not in profile:
            profile["commute_results"] = {}
        profile["commute_results"][provider_key] = {
            "segments": result["segments"],
            "total_m": result["total_m"],
            "total_s": result["total_s"],
            "segment_routes": result["segment_routes"],
        }
        profile["last_commute_provider"] = provider_key
        profile["infra"] = infra
        status_summary = (
            f"Updated {datetime.now().strftime('%H:%M:%S')} via {provider_key} · "
            f"{result['total_m'] / 1609.344:,.2f} mi · {result['total_s'] / 60.0:,.1f} min"
        )
        st.session_state[status_key] = status_summary
        st.session_state[progress_key] = 100
        st.session_state[progress_label_key] = "Done"
        status_placeholder.caption(st.session_state[status_key])
        status_progress.progress(
            st.session_state[progress_key],
            text=st.session_state[progress_label_key],
        )

        st.session_state["commute_profiles"][profile_key] = profile
        st.rerun()

    # ---------------------------------------------
    # Provider Tabs with Persistent Storage
    # ---------------------------------------------
    st.subheader("Route Analysis by Provider")

    # Define location marker styles for legend
    LOCATION_MARKERS = {
        "house": {"color": "green", "icon": "home", "label": "Home"},
        "work": {"color": "blue", "icon": "briefcase", "label": "Work"},
        "daycare": {"color": "purple", "icon": "child", "label": "Daycare"},
        "school": {"color": "orange", "icon": "graduation-cap", "label": "School"},
        "gym": {"color": "red", "icon": "heart", "label": "Gym"},
        "grocery": {"color": "cadetblue", "icon": "shopping-cart", "label": "Grocery"},
        "default": {"color": "gray", "icon": "map-marker", "label": "Location"},
    }

    # Create tabs for each provider - put last computed provider first
    last_provider = profile.get("last_commute_provider")
    if last_provider == "Google":
        tab_order = ["Google", "ORS", "Waze"]
    elif last_provider == "Waze":
        tab_order = ["Waze", "ORS", "Google"]
    else:
        tab_order = ["ORS", "Google", "Waze"]

    provider_tabs = st.tabs(tab_order)
    tab_map = dict(zip(tab_order, provider_tabs))

    def get_marker_style(label: str) -> dict:
        """Get marker style based on location label."""
        label_lower = label.strip().lower()
        for key, style in LOCATION_MARKERS.items():
            if key in label_lower:
                return style
        return LOCATION_MARKERS["default"]

    def build_legend_html(locations: list[dict]) -> str:
        """Build HTML for map legend."""
        legend_items = []
        seen_types = set()
        for loc in locations:
            style = get_marker_style(loc["label"])
            type_key = style["label"]
            if type_key not in seen_types:
                seen_types.add(type_key)
                legend_items.append(
                    f'<div style="display:flex;align-items:center;margin:4px 0;">'
                    f'<i class="fa fa-{style["icon"]}" style="color:{style["color"]};'
                    f'font-size:14px;width:20px;"></i>'
                    f'<span style="margin-left:6px;font-size:12px;">{type_key}</span>'
                    f'</div>'
                )
        return (
            '<div style="position:fixed;bottom:30px;left:10px;z-index:1000;'
            'background:white;padding:10px;border-radius:5px;'
            'box-shadow:0 0 5px rgba(0,0,0,0.3);max-width:150px;">'
            '<div style="font-weight:bold;margin-bottom:6px;font-size:13px;">Legend</div>'
            + "".join(legend_items)
            + '</div>'
        )

    def render_provider_map(provider_key: str, profile: dict, locations: list[dict], home_label: str):
        """Render map for a specific provider."""
        res = profile.get("commute_results", {}).get(provider_key)

        if not res:
            st.info(f"No {provider_key} route computed yet. Select {provider_key} as Routing Method and click Compute Commute.")
            return

        # Show results summary
        st.dataframe(pd.DataFrame(res["segments"]), width="stretch", hide_index=True)

        col1, col2 = st.columns(2)
        with col1:
            st.metric("Total Distance", f"{res['total_m'] / 1609.344:,.2f} mi")
        with col2:
            st.metric("Total Drive Time", f"{res['total_s'] / 60.0:,.1f} min")

        # Build map
        m = folium.Map(
            location=[39.8283, -98.5795],
            zoom_start=4,
            tiles="OpenStreetMap",
        )

        bounds = []

        # Add location markers and their coordinates to bounds
        for loc in locations:
            bounds.append([loc["lat"], loc["lon"]])
            style = get_marker_style(loc["label"])

            folium.Marker(
                location=[loc["lat"], loc["lon"]],
                popup=f"<b>{loc['label']}</b><br>{loc['address']}",
                icon=folium.Icon(
                    color=style["color"],
                    icon=style["icon"],
                    prefix="fa",
                ),
            ).add_to(m)

        # Add route points to bounds for proper zoom
        for seg in res.get("segment_routes", []):
            pts = seg.get("points", [])
            for pt in pts:
                if pt and len(pt) >= 2:
                    bounds.append([pt[0], pt[1]])

        # Fit bounds with padding
        if bounds:
            m.fit_bounds(bounds, padding=(20, 20))

        # Add route segments
        for seg in res.get("segment_routes", []):
            pts = seg["points"]
            if not pts:
                continue

            is_return_leg = seg.get("is_return_leg", False)
            layer_name = f"{provider_key}: {seg['label']}"

            leg_layer = folium.FeatureGroup(name=layer_name, show=True)

            if is_return_leg:
                folium.PolyLine(
                    locations=pts,
                    color="#000000",
                    weight=5,
                    opacity=0.9,
                    tooltip=layer_name,
                ).add_to(leg_layer)
            else:
                folium.PolyLine(
                    locations=pts,
                    color="#000000",
                    weight=7,
                    opacity=0.5,
                ).add_to(leg_layer)
                folium.PolyLine(
                    locations=pts,
                    color="#FFFFFF",
                    weight=4,
                    opacity=0.95,
                    tooltip=layer_name,
                ).add_to(leg_layer)

            leg_layer.add_to(m)

        # Add infrastructure events if available
        infra = profile.get("infra") or {}
        infra_events = infra.get("events", [])
        if infra_events:
            infra_layer = folium.FeatureGroup(name="Infrastructure Events", show=True)
            for event in infra_events:
                geom = event.get("geometry") or {}
                coords = geom.get("coordinates") or []
                if not coords:
                    continue
                event_type = event.get("event_type")
                if event_type == "jam":
                    folium.PolyLine(
                        locations=[[lat, lon] for lon, lat in coords],
                        color="#FF6F00",
                        weight=4,
                        opacity=0.8,
                        tooltip=event.get("description"),
                    ).add_to(infra_layer)
                else:
                    marker_color = "orange" if event_type == "construction" else "red"
                    folium.CircleMarker(
                        location=[coords[1], coords[0]],
                        radius=6,
                        color=marker_color,
                        fill=True,
                        fill_color=marker_color,
                        tooltip=event.get("description"),
                    ).add_to(infra_layer)
            infra_layer.add_to(m)

        # Add legend
        legend_html = build_legend_html(locations)
        m.get_root().html.add_child(folium.Element(legend_html))

        folium.LayerControl(collapsed=False).add_to(m)
        st_folium(m, width=900, height=500, key=f"map_{profile_key}_{provider_key}")

    with tab_map["ORS"]:
        render_provider_map("ORS", profile, locations, home_label)

    with tab_map["Google"]:
        render_provider_map("Google", profile, locations, home_label)

    with tab_map["Waze"]:
        render_provider_map("Waze", profile, locations, home_label)

    infra = profile.get("infra") or {}
    if infra:
        with st.expander("Infrastructure Support Layer", expanded=False):
            summary = infra.get("summary", {})
            fetched_at = infra.get("fetched_at")
            st.markdown(
                f"**Commute Reliability:** {summary.get('incidents', 0)} incidents · "
                f"{summary.get('jams', 0)} jams"
            )
            if fetched_at:
                st.caption(f"Data pulled at: {fetched_at}")
            infra_events = infra.get("events", [])
            if infra_events:
                incident_rows = []
                for event in infra_events:
                    incident_rows.append(
                        {
                            "Type": event.get("event_type"),
                            "Description": event.get("description"),
                            "Severity": event.get("severity"),
                            "Source": event.get("source"),
                        }
                    )
                st.dataframe(
                    pd.DataFrame(incident_rows),
                    width="stretch",
                    hide_index=True,
                )


def render_commute():
    with st.expander(
        "Commute Analysis",
        expanded=st.session_state["commute_expanded"]
    ):
        locations = st.session_state.get("map_data", {}).get("locations", [])
        profiles = st.session_state.get("commute_profiles", {})

        if not profiles:
            profiles["Primary"] = _init_commute_profile("Primary", locations)
            st.session_state["commute_profiles"] = profiles

        new_name = st.text_input("New Commute Name", key="new_commute_name")
        if st.button("Add Commute", key="add_commute"):
            if not new_name.strip():
                st.error("Commute name cannot be blank.")
            elif new_name in profiles:
                st.warning("Commute name already exists.")
            else:
                profiles[new_name] = _init_commute_profile(new_name, locations)
                st.session_state["commute_profiles"] = profiles
                st.session_state["commute_active_tab"] = new_name
                st.rerun()

        tab_labels = list(profiles.keys())
        active_tab = st.session_state.get("commute_active_tab")
        if active_tab not in tab_labels:
            active_tab = tab_labels[0] if tab_labels else None

        if tab_labels:
            selected_tab = st.radio(
                "Commute Tabs",
                tab_labels,
                index=tab_labels.index(active_tab) if active_tab in tab_labels else 0,
                horizontal=True,
                key="commute_tab_selector",
                label_visibility="collapsed",
            )
            st.session_state["commute_active_tab"] = selected_tab

            _render_commute_tab(selected_tab, profiles[selected_tab], locations)
            if st.button("Save Commute", key=f"save_commute_{selected_tab}"):
                try:
                    save_path = save_current_profile()
                    st.success(f"Saved to {save_path}")
                except Exception as exc:
                    st.error(f"Save failed: {exc}")
