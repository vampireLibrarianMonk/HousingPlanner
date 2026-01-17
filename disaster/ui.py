import streamlit as st
import folium
from streamlit_folium import st_folium
import math
import pyproj
from shapely.geometry import Point, shape
from shapely.ops import transform
from collections import defaultdict
import pandas as pd

from locations.logic import _get_loc_by_label

from .flood import (
    fetch_fema_flood_zones,
    flood_zone_style,
    flood_zone_at_point,
    FEMA_ZONE_EXPLANATIONS, zone_descriptions,
)

from .wildfire import (
    fetch_mtbs_kmz,
    extract_geometry_kml,
    parse_kml_geometries,
)

from .declarations import (
    reverse_geocode_county_state,
    fetch_fema_disaster_declarations,
    _to_fema_designated_area_from_county,
)


def render_disaster():
    with st.expander(
            "🌪️ Disaster Risk & Hazard Mapping",
            expanded=st.session_state["disaster_expanded"],
    ):
        st.subheader("Disaster Map (Home of Record)")

        locations = st.session_state["map_data"]["locations"]
        house = _get_loc_by_label(locations, "House")

        if not house:
            st.warning("Add a location labeled **House** to enable disaster mapping.")
        else:
            radius_miles = st.slider(
                "Search radius (miles)",
                min_value=1,
                max_value=50,
                step=1,
                key="disaster_radius_miles",
            )

            # ---------------------------------------
            # Planned hazard layers (toggle to enable)
            # ---------------------------------------
            st.markdown("### Planned hazard layers (toggle to enable)")

            c1, c2 = st.columns(2)

            with c1:
                st.checkbox("Flood zones (FEMA)", key="hz_flood")
                st.checkbox("Wildfire risk", key="hz_wildfire")
                st.checkbox("Historical disaster declarations", key="hz_disaster_history")

            with c2:
                st.checkbox("Heat risk", key="hz_heat")
                st.checkbox("Earthquake fault proximity", key="hz_earthquake")
                st.checkbox("Hurricane / wind exposure", key="hz_wind")

            st.divider()

            # ---------------------------------------
            # Disaster map
            # ---------------------------------------
            m = folium.Map(
                location=[house["lat"], house["lon"]],
                zoom_start=13,
                tiles="OpenStreetMap",
            )

            folium.Marker(
                location=[house["lat"], house["lon"]],
                popup=f"<b>House</b><br>{house['address']}",
                icon=folium.Icon(color="red", icon="home"),
            ).add_to(m)

            # ---------------------------------------
            # Build search radius geometry (single source of truth)
            # ---------------------------------------
            project = pyproj.Transformer.from_crs(
                "EPSG:4326", "EPSG:3857", always_xy=True
            ).transform

            house_point_m = transform(project, Point(house["lon"], house["lat"]))
            radius_meters = radius_miles * 1609.34

            # THIS must exist before any use
            search_area = house_point_m.buffer(radius_meters)

            # ---------------------------------------
            # Draw exact search radius (same geometry as clip)
            # ---------------------------------------
            search_area_latlon = transform(
                pyproj.Transformer.from_crs(
                    "EPSG:3857", "EPSG:4326", always_xy=True
                ).transform,
                search_area,
            )

            folium.GeoJson(
                search_area_latlon.__geo_interface__,
                name=f"{radius_miles} mile search radius",
                style_function=lambda _: {
                    "color": "#1565C0",
                    "weight": 2,
                    "fill": False,
                    "dashArray": "6,6",
                },
                control=False,  # radius is informational, not a toggle
            ).add_to(m)

            # ---------------------------------------
            # FEMA Flood Hazard Zones (NFHL FeatureServer)
            # ---------------------------------------
            # NOTE:
            # FEMA flood data is cached per search radius.
            # Map panning/zooming does NOT trigger refetches.
            if not st.session_state.get("hz_flood"):
                st.session_state.pop("fema_flood_geojson", None)
                st.session_state.pop("fema_radius_key", None)

            if not st.session_state.get("hz_wildfire"):
                st.session_state.pop("wildfire_geoms", None)
                st.session_state.pop("wildfire_radius_key", None)

            if not st.session_state.get("hz_disaster_history"):
                st.session_state.pop("fema_disaster_history_last", None)

            # ---------------------------------------
            # Build radius-based bounding box (GIS-safe)
            # ---------------------------------------
            meters_per_degree_lat = 111_320
            meters_per_degree_lon = 111_320 * math.cos(math.radians(house["lat"]))

            delta_lat = radius_miles * 1609.34 / meters_per_degree_lat
            delta_lon = radius_miles * 1609.34 / meters_per_degree_lon

            bbox = (
                house["lon"] - delta_lon,
                house["lat"] - delta_lat,
                house["lon"] + delta_lon,
                house["lat"] + delta_lat,
            )

            bbox_key = (
                round(house["lat"], 4),
                round(house["lon"], 4),
                round(radius_miles, 2),
            )

            if st.session_state.get("hz_flood"):
                # ---------------------------------------
                # Cache FEMA fetch by radius ONLY
                # ---------------------------------------
                if (
                        "fema_flood_geojson" not in st.session_state
                        or st.session_state.get("fema_radius_key") != bbox_key
                ):
                    st.session_state["fema_flood_geojson"] = fetch_fema_flood_zones(bbox)
                    st.session_state["fema_radius_key"] = bbox_key

                flood_geojson = st.session_state["fema_flood_geojson"]

                zone_groups = defaultdict(list)

                # ---------------------------------------
                # Clip FEMA features to circular radius
                # ---------------------------------------
                zone_groups = defaultdict(list)

                for feature in flood_geojson["features"]:
                    props = feature.get("properties")
                    geom = feature.get("geometry")

                    if not props or not geom:
                        continue

                    polygon_m = transform(project, shape(geom))

                    if polygon_m.intersects(search_area):
                        clipped_geom = polygon_m.intersection(search_area)

                        if not clipped_geom.is_empty:
                            zone = props.get("FLD_ZONE")
                            if zone:
                                clipped_feature = {
                                    "type": "Feature",
                                    "properties": props,
                                    "geometry": transform(
                                        pyproj.Transformer.from_crs(
                                            "EPSG:3857", "EPSG:4326", always_xy=True
                                        ).transform,
                                        clipped_geom,
                                    ).__geo_interface__,
                                }

                                zone_groups[zone].append(clipped_feature)

                for zone, features in zone_groups.items():
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": features,
                        },
                        name=f"Flood Zone {zone}",
                        style_function=flood_zone_style,
                        tooltip=folium.GeoJsonTooltip(
                            fields=["FLD_ZONE", "SFHA_TF"],
                            aliases=["Flood Zone", "SFHA"],
                            sticky=True,
                        ),
                        control=True,  # enables legend toggle
                        show=True if zone != "X" else False,  # hide Zone X by default
                    ).add_to(m)

            if st.session_state.get("hz_wildfire"):
                # ---------------------------------------
                # Fetch + cache wildfire items
                # ---------------------------------------
                if (
                        "wildfire_items" not in st.session_state
                        or st.session_state.get("wildfire_radius_key") != bbox_key
                ):
                    kmz = fetch_mtbs_kmz(bbox)
                    kml_text = extract_geometry_kml(kmz)

                    # 🔑 geometry + metadata together (single source of truth)
                    wildfire_items = parse_kml_geometries(kml_text)

                    # st.caption(
                    #     "DEBUG MTBS wildfire items sample: "
                    #     f"{wildfire_items[:3]}"
                    # )

                    st.session_state["wildfire_items"] = wildfire_items
                    st.session_state["wildfire_radius_key"] = bbox_key

                # ---------------------------------------
                # Build clipped wildfire features
                # ---------------------------------------
                wildfire_features = []

                to_3857 = pyproj.Transformer.from_crs(
                    "EPSG:4326", "EPSG:3857", always_xy=True
                ).transform

                to_4326 = pyproj.Transformer.from_crs(
                    "EPSG:3857", "EPSG:4326", always_xy=True
                ).transform

                for item in st.session_state["wildfire_items"]:
                    geom = item["geometry"]
                    fire_year = item["fire_year"]
                    fire_name = item["fire_name"]
                    fire_id = item["fire_id"]

                    if fire_year is None:
                        continue

                    geom_m = transform(to_3857, geom)

                    if geom_m.is_empty:
                        continue

                    if not geom_m.is_valid:
                        geom_m = geom_m.buffer(0)

                    if not geom_m.intersects(search_area):
                        continue

                    clipped = geom_m.intersection(search_area)

                    if clipped.is_empty:
                        continue

                    wildfire_features.append({
                        "type": "Feature",
                        "properties": {
                            "fire_year": fire_year,
                            "fire_name": fire_name,
                            "fire_id": fire_id,
                        },
                        "geometry": transform(to_4326, clipped).__geo_interface__,
                    })

                # ---------------------------------------
                # Group wildfire features by fire year
                # ---------------------------------------
                wildfire_by_year = defaultdict(list)

                for feature in wildfire_features:
                    wildfire_by_year[
                        feature["properties"]["fire_year"]
                    ].append(feature)

                # ---------------------------------------
                # Add one toggleable layer per fire year
                # ---------------------------------------
                if wildfire_by_year:
                    newest_year = max(wildfire_by_year)

                    for year, features in sorted(wildfire_by_year.items()):
                        folium.GeoJson(
                            {
                                "type": "FeatureCollection",
                                "features": features,
                            },
                            name=f"Wildfires – {year}",
                            style_function=lambda _: {
                                "color": "#D84315",
                                "weight": 1.4,
                                "dashArray": "4,4",
                                "fillColor": "#FF8A65",
                                "fillOpacity": 0.22,
                            },
                            tooltip=folium.GeoJsonTooltip(
                                fields=["fire_name", "fire_year", "fire_id"],
                                aliases=["Fire Name", "Year", "MTBS ID"],
                                sticky=True,
                            ),
                            control=True,
                            show=(year == newest_year),  # newest on by default
                        ).add_to(m)

                # ---------------------------------------
                # Wildfire legend (map overlay)
                # ---------------------------------------
                if wildfire_features:
                    wildfire_legend_html = """
                    <div style="
                        position: fixed;
                        bottom: 35px;
                        left: 35px;
                        z-index: 9999;
                        background: white;
                        padding: 10px 14px;
                        border-radius: 6px;
                        box-shadow: 0 2px 6px rgba(0,0,0,0.3);
                        font-size: 13px;
                    ">
                        <b>Historical Wildfires (MTBS)</b><br>
                        <span style="
                            display:inline-block;
                            width:14px;
                            height:14px;
                            background:#FF8A65;
                            border:1px solid #D84315;
                            margin-right:6px;">
                        </span>
                        Burned Area Perimeter<br>
                        <span style="font-size:11px;color:#555;">
                            Historical large fires only
                        </span>
                    </div>
                    """

                    m.get_root().html.add_child(
                        folium.Element(wildfire_legend_html)
                    )

            folium.LayerControl(
                collapsed=False,
                position="topright",
            ).add_to(m)

            st_folium(
                m,
                width=900,
                height=500,
                returned_objects=[],
            )

            if st.session_state.get("hz_flood"):
                st.caption("Flood data source: FEMA National Flood Hazard Layer (NFHL)")

            st.divider()
            st.markdown("### Enabled layers:")

            enabled = []

            flood_geojson = st.session_state.get("fema_flood_geojson")

            if st.session_state.get("hz_flood") and flood_geojson and flood_geojson["features"]:
                house_zone, house_sfha = flood_zone_at_point(
                    flood_geojson,
                    house["lat"],
                    house["lon"],
                )

                if house_zone:
                    zone_info = FEMA_ZONE_EXPLANATIONS.get(house_zone)

                    if zone_info:
                        st.markdown(
                            f"""
            ### {zone_info['title']}

            - **Summary:** {zone_info['summary']}
            - **Flood insurance:** {zone_info['insurance']}
            - **SFHA:** {"Yes" if house_sfha == "T" else "No"}
            - **Determination:** Flood zone determined at the house location.
            """,
                            unsafe_allow_html=True,
                        )

            if st.session_state.get("hz_wildfire"):
                if wildfire_features:
                    fire_years = [
                        f["properties"]["fire_year"]
                        for f in wildfire_features
                        if f["properties"].get("fire_year") is not None
                    ]

                    most_recent_year = max(fire_years) if fire_years else "unknown"
                    fire_count = len(wildfire_features)

                    st.markdown(
                        f"""
            ### Historical Wildfire Context

            - **{fire_count} historical wildfire perimeter(s)** intersect the area within **{radius_miles} miles**
            - **Most recent recorded fire year:** {most_recent_year}
            - These represent **past burned areas**, not current fire conditions
            - Presence does **not** indicate future wildfire likelihood

            Wildfire data source: USGS / USDA MTBS
            """
                    )
                else:
                    st.markdown(
                        f"""
            ### Historical Wildfire Context

            - **No mapped historical wildfire perimeters** intersect the area within **{radius_miles} miles**
            - This reflects **recorded large-fire history only**
            - Absence of recorded perimeters does **not guarantee zero wildfire risk**

            Wildfire data source: USGS / USDA MTBS
            """
                    )

            if st.session_state.get("hz_disaster_history"):
                county, state_abbrev = reverse_geocode_county_state(
                    house["lat"],
                    house["lon"],
                    address_fallback=house.get("address"),
                )

                designated_area = _to_fema_designated_area_from_county(county or "")

                data = fetch_fema_disaster_declarations(
                    state_abbrev=state_abbrev or "",
                    designated_area=designated_area,
                    top=100,
                )

                meta = data.get("metadata", {}) or {}
                rows = data.get("DisasterDeclarationsSummaries", []) or []

                if rows:
                    st.markdown(
                        f"""
    ### Historical Disaster Declarations (County-Level)

    - **County (FEMA designatedArea):** {designated_area}
    - **State:** {state_abbrev}
    - **Records returned:** {len(rows)}
    - **Sort:** declarationDate (newest → oldest)
    """
                    )

                    df = pd.DataFrame(rows)

                    # Keep a clean “buyer-readable” view, while retaining raw metadata in an expander
                    keep_cols = [
                        "declarationDate",
                        "femaDeclarationString",
                        "declarationType",
                        "incidentType",
                        "declarationTitle",
                        "incidentBeginDate",
                        "incidentEndDate",
                        "disasterCloseoutDate",
                        "fyDeclared",
                        "paProgramDeclared",
                        "iaProgramDeclared",
                        "ihProgramDeclared",
                        "hmProgramDeclared",
                        "designatedArea",
                        "state",
                        "disasterNumber",
                    ]

                    existing = [c for c in keep_cols if c in df.columns]
                    df_view = df[existing].copy()

                    # Ensure newest-first (even if API changes behavior)
                    if "declarationDate" in df_view.columns:
                        df_view["declarationDate"] = pd.to_datetime(df_view["declarationDate"], errors="coerce")
                        df_view = df_view.sort_values("declarationDate", ascending=False)
                        df_view["declarationDate"] = df_view["declarationDate"].dt.date.astype(str)

                    # Light cleanup for date columns
                    for c in ["incidentBeginDate", "incidentEndDate", "disasterCloseoutDate"]:
                        if c in df_view.columns:
                            d = pd.to_datetime(df_view[c], errors="coerce")
                            df_view[c] = d.dt.date.astype("string")

                    st.dataframe(df_view, width="stretch", hide_index=True)

                    with st.expander("FEMA API metadata (debug)", expanded=False):
                        st.json(meta)
                else:
                    st.markdown(
                        f"""
    ### Historical Disaster Declarations (County-Level)

    - **County (FEMA designatedArea):** {designated_area or "Unknown"}
    - **State:** {state_abbrev or "Unknown"}
    - **Result:** No records returned from FEMA for this county filter.
    """
                    )

                    nearby_zones = set()

            nearby_zones = set()

            flood_geojson = st.session_state.get("fema_flood_geojson")

            if flood_geojson:
                for feature in flood_geojson["features"]:
                    props = feature.get("properties")
                    geom = feature.get("geometry")

                    if not props or not geom:
                        continue

                    polygon_m = transform(project, shape(geom))

                    # True spatial test (not distance-only)
                    if polygon_m.intersects(search_area):
                        zone = props.get("FLD_ZONE")
                        if zone:
                            nearby_zones.add(zone)

            nearby_zones = sorted(nearby_zones)

            descriptions = [
                f"{zone_descriptions[z]} (Zone {z})"
                for z in nearby_zones
                if z in zone_descriptions and z != house_zone
            ]

            if descriptions:
                st.markdown(
                    f"""
                <strong>Nearby flood risk context (within {radius_miles} miles):</strong><br>
                Surrounding areas include {", ".join(sorted(set(descriptions)))}.<br><br>
                These nearby flood zones do not change the flood classification at this property.
                """,
                    unsafe_allow_html=True,
                )

            if st.session_state["hz_earthquake"]:
                enabled.append("Earthquake fault proximity")

            if st.session_state["hz_wind"]:
                enabled.append("Hurricane / wind exposure")

            if st.session_state["hz_heat"]:
                enabled.append("Heat risk")

            for name in enabled:
                st.info(f"**{name}** is enabled — data integration will be wired in next.")
