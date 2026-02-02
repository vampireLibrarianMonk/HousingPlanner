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

from .declarations import (
    reverse_geocode_county_state,
    fetch_fema_disaster_declarations,
    _to_fema_designated_area_from_county,
)

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

from .heat import (
    fetch_heatrisk_point,
    fetch_heatrisk_raster,
    heatrisk_legend_items,
    fetch_nws_heat_alerts,
    fetch_historical_heat_events,
    fetch_heat_event_geojson,
)

from .wind import fetch_wind_assessment
from .wind import fetch_wind_layers


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

            if not st.session_state.get("hz_heat"):
                st.session_state.pop("heatrisk_cache_key", None)
                st.session_state.pop("heatrisk_raster", None)
                st.session_state.pop("heatrisk_point", None)

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

            wind_layers = None

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

            # ---------------------------------------
            # Heat Risk (NOAA HeatRisk ImageServer)
            # ---------------------------------------
            heatrisk_summary = None
            heat_alerts = None
            heat_history = None
            heat_history_polygons = None
            if st.session_state.get("hz_heat"):
                heatrisk_key = (
                    round(house["lat"], 4),
                    round(house["lon"], 4),
                    round(radius_miles, 2),
                )

                if (
                    "heatrisk_raster" not in st.session_state
                    or st.session_state.get("heatrisk_cache_key") != heatrisk_key
                ):
                    st.session_state["heatrisk_raster"] = fetch_heatrisk_raster(bbox)
                    st.session_state["heatrisk_point"] = fetch_heatrisk_point(
                        house["lat"],
                        house["lon"],
                    )
                    st.session_state["heatrisk_alerts"] = fetch_nws_heat_alerts(
                        house["lat"],
                        house["lon"],
                    )
                    st.session_state["heatrisk_history"] = fetch_historical_heat_events(
                        house["lat"],
                        house["lon"],
                        days=365,
                    )
                    st.session_state["heatrisk_history_polygons"] = []
                    st.session_state["heatrisk_cache_key"] = heatrisk_key

                heatrisk_raster = st.session_state.get("heatrisk_raster")
                heatrisk_summary = st.session_state.get("heatrisk_point")
                heat_alerts = st.session_state.get("heatrisk_alerts")
                heat_history = st.session_state.get("heatrisk_history")
                heat_history_polygons = st.session_state.get("heatrisk_history_polygons")

                if heatrisk_raster and heatrisk_raster.get("href") and heatrisk_summary:
                    folium.raster_layers.ImageOverlay(
                        image=heatrisk_raster["href"],
                        bounds=[
                            [bbox[1], bbox[0]],
                            [bbox[3], bbox[2]],
                        ],
                        name="HeatRisk (NWS)",
                        opacity=0.55,
                        interactive=False,
                        cross_origin=False,
                        zindex=4,
                    ).add_to(m)

                    mask_polygon = {
                        "type": "Feature",
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [
                                [
                                    [-180, -90],
                                    [180, -90],
                                    [180, 90],
                                    [-180, 90],
                                    [-180, -90],
                                ],
                                list(search_area_latlon.exterior.coords),
                            ],
                        },
                        "properties": {},
                    }

                    folium.GeoJson(
                        mask_polygon,
                        name="HeatRisk Mask",
                        style_function=lambda _: {
                            "fillColor": "#FFFFFF",
                            "fillOpacity": 0.0,
                            "color": "#FFFFFF",
                            "weight": 0,
                            "fillRule": "evenodd",
                        },
                        control=False,
                    ).add_to(m)

                if heat_history and heat_history_polygons is not None:
                    if not heat_history_polygons:
                        for event in heat_history:
                            geojson = fetch_heat_event_geojson(event)
                            if not geojson:
                                continue

                            for feature in geojson.get("features", []) or []:
                                geom = feature.get("geometry")
                                if not geom:
                                    continue

                                polygon_m = transform(project, shape(geom))
                                if not polygon_m.intersects(search_area):
                                    continue

                                clipped_geom = polygon_m.intersection(search_area)
                                if clipped_geom.is_empty:
                                    continue

                                heat_history_polygons.append({
                                    "type": "Feature",
                                    "properties": {
                                        "name": event.get("name"),
                                        "issue": event.get("issue"),
                                        "expire": event.get("expire"),
                                    },
                                    "geometry": transform(
                                        pyproj.Transformer.from_crs(
                                            "EPSG:3857", "EPSG:4326", always_xy=True
                                        ).transform,
                                        clipped_geom,
                                    ).__geo_interface__,
                                })

                        st.session_state["heatrisk_history_polygons"] = heat_history_polygons

                    if heat_history_polygons:
                        folium.GeoJson(
                            {
                                "type": "FeatureCollection",
                                "features": heat_history_polygons,
                            },
                            name="Heat Advisory History (12 mo)",
                            style_function=lambda _: {
                                "color": "#E65100",
                                "weight": 2,
                                "fillColor": "#FF9800",
                                "fillOpacity": 0.25,
                            },
                            tooltip=folium.GeoJsonTooltip(
                                fields=["name", "issue", "expire"],
                                aliases=["Event", "Issue", "Expire"],
                                sticky=True,
                            ),
                            control=True,
                            show=False,
                        ).add_to(m)


            # ---------------------------------------
            # Wind geometry layers (polygons + lines)
            # ---------------------------------------
            if st.session_state.get("hz_wind"):
                wind_layers = fetch_wind_layers(
                    house["lat"],
                    house["lon"],
                    search_area,
                    bbox=bbox,
                )

                # Hurricane wind swaths (polygons)
                swath_styles = {
                    "64kt": {"color": "#B71C1C", "fillColor": "#B71C1C", "fillOpacity": 0.35},
                    "50kt": {"color": "#E53935", "fillColor": "#E53935", "fillOpacity": 0.30},
                    "34kt": {"color": "#FB8C00", "fillColor": "#FB8C00", "fillOpacity": 0.25},
                }

                for level, features in wind_layers["wind_swaths"].items():
                    if features:
                        folium.GeoJson(
                            {
                                "type": "FeatureCollection",
                                "features": features,
                            },
                            name=f"Hurricane wind swath – {level}",
                            style_function=lambda _, s=swath_styles[level]: s,
                            control=True,
                            show=False,
                        ).add_to(m)

                # Hurricane tracks (lines)
                if wind_layers["tracks"]:
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": wind_layers["tracks"],
                        },
                        name="Hurricane tracks",
                        style_function=lambda _: {
                            "color": "#6A1B9A",
                            "weight": 2.5,
                        },
                        control=True,
                        show=False,
                    ).add_to(m)

            # ---------------------------------------
            # Wind screening assessment (NOAA vs ASCE)
            # ---------------------------------------
            wind_assessment = None
            if st.session_state.get("hz_wind"):
                try:
                    wind_assessment = fetch_wind_assessment(
                        house["lat"], house["lon"], bbox=bbox
                    )
                except Exception:
                    wind_assessment = None
                # Tornado paths (lines)
                if wind_layers["tornado_paths"]:
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": wind_layers["tornado_paths"],
                        },
                        name="Tornado paths",
                        style_function=lambda _: {
                            "color": "#283593",
                            "weight": 2,
                            "dashArray": "6,4",
                        },
                        control=True,
                        show=False,
                    ).add_to(m)

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

            if st.session_state.get("hz_wind"):
                if wind_assessment:
                    if wind_assessment.get("asce_available"):
                        asce = wind_assessment.get("asce") or {}
                        st.markdown(
                            f"""
            ### ASCE 7 Design Wind Speed (Licensed)

            - **Design wind speed (MRI 700/300):** {asce.get('design_wind_speed_mph') or 'Not available'}
            - **Risk category:** {asce.get('risk_category', 'II')}
            - **Standard:** {asce.get('standard', '7-16')}
            - **Hurricane-prone region:** {'Yes' if asce.get('is_hurricane_prone') else 'No'}

            ASCE data is **design-level** and requires a licensed API token.
            """
                        )
                    else:
                        st.markdown(
                            f"""
            ### Wind & Hurricane Exposure (Screening Level)

            - **Wind exposure category:** {wind_assessment['screening_wind_category']}
            - **Hurricane-force winds recorded:** {'Yes' if wind_assessment['hurricane_force_winds'] else 'No'}
            - **Overall risk tier:** {wind_assessment['risk_tier']}
            - **Data source:** {wind_assessment['source']}
            - **Note:** {wind_assessment.get('note', '')}

            This is a **screening-level** view based on historical NOAA layers. It does
            **not** represent code design wind speeds.
            """
                        )
                else:
                    st.markdown(
                        """
            ### Wind & Hurricane Screening (NOAA)

            - No NOAA wind assessment available for this location.
            """
                    )

                if wind_layers:
                    missing_layers = []
                    swaths = wind_layers.get("wind_swaths", {})
                    if not swaths.get("64kt"):
                        missing_layers.append("Hurricane-force wind swath (64 kt)")
                    if not swaths.get("50kt"):
                        missing_layers.append("Strong wind swath (50 kt)")
                    if not swaths.get("34kt"):
                        missing_layers.append("Tropical storm wind swath (34 kt)")
                    if not wind_layers.get("tracks"):
                        missing_layers.append("Hurricane track")
                    if not wind_layers.get("tornado_paths"):
                        missing_layers.append("Tornado path")

                    if missing_layers:
                        st.info(
                            "Searched within this radius but found no data for: "
                            + ", ".join(missing_layers)
                        )

            if st.session_state.get("hz_wind"):
                wind_legend_html = """
                <div style="
                    position: fixed;
                    bottom: 35px;
                    right: 35px;
                    z-index: 9999;
                    background: white;
                    padding: 10px 14px;
                    border-radius: 6px;
                    box-shadow: 0 2px 6px rgba(0,0,0,0.3);
                    font-size: 13px;
                ">
                    <b>Wind & Hurricane Layers</b><br>

                    <span style="display:inline-block;width:14px;height:14px;
                        background:#B71C1C;margin-right:6px;"></span>
                    Hurricane-force wind (64 kt)<br>

                    <span style="display:inline-block;width:14px;height:14px;
                        background:#E53935;margin-right:6px;"></span>
                    Strong wind (50 kt)<br>

                    <span style="display:inline-block;width:14px;height:14px;
                        background:#FB8C00;margin-right:6px;"></span>
                    Tropical storm wind (34 kt)<br>

                    <svg width="14" height="10" style="margin-right:6px;">
                        <line x1="0" y1="5" x2="14" y2="5"
                              stroke="#6A1B9A" stroke-width="2"/>
                    </svg>
                    Hurricane track<br>

                    <svg width="14" height="10" style="margin-right:6px;">
                        <line x1="0" y1="5" x2="14" y2="5"
                              stroke="#283593" stroke-width="2"
                              stroke-dasharray="4,3"/>
                    </svg>
                    Tornado path
                </div>
                """

                m.get_root().html.add_child(folium.Element(wind_legend_html))

            if st.session_state.get("hz_heat") and heatrisk_summary:
                legend_rows = "".join(
                    f"<span style='display:inline-block;width:14px;height:14px;"
                    f"background:{color};margin-right:6px;'></span>{label}<br>"
                    for label, color in heatrisk_legend_items()
                )
                heat_legend_html = f"""
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
                    <b>HeatRisk (NWS)</b><br>
                    {legend_rows}
                </div>
                """
                m.get_root().html.add_child(folium.Element(heat_legend_html))

            if st.session_state.get("hz_heat") and heat_history_polygons:
                heat_history_legend_html = """
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
                    <b>Heat Advisory History (12 mo)</b><br>
                    <span style="display:inline-block;width:14px;height:14px;
                        background:#FF9800;border:1px solid #E65100;margin-right:6px;"></span>
                    Historical advisory polygons
                </div>
                """
                m.get_root().html.add_child(folium.Element(heat_history_legend_html))

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

            if st.session_state.get("hz_heat"):
                if heatrisk_summary:
                    st.markdown(
                        f"""
            ### Heat Risk (Screening Level)

            - **HeatRisk category:** {heatrisk_summary.get('label', 'Unknown')}
            - **Data source:** NOAA/NWS HeatRisk (experimental)
            """
                    )
                else:
                    st.markdown(
                        """
            ### Heat Risk (Screening Level)

            - No HeatRisk value returned for this location (NoData).
            - HeatRisk data is time-limited; check again during active forecast windows.
            """
                    )

                if heat_alerts:
                    active_alerts = heat_alerts.get("active", [])
                    if active_alerts:
                        st.markdown("**Active Heat Advisories:**")
                        for alert in active_alerts:
                            st.markdown(
                                f"- {alert['event']} ({alert.get('severity')}) — "
                                f"{alert.get('effective')} → {alert.get('expires')}"
                            )
                    else:
                        st.markdown("**Active Heat Advisories:** None")

                if heat_history is not None:
                    if heat_history:
                        st.markdown(
                            f"**Heat Advisory Snapshot (past 12 months):** {len(heat_history)} event(s)"
                        )
                        history_df = pd.DataFrame(heat_history)
                        display_cols = [
                            "name",
                            "issue",
                            "expire",
                            "phenomena",
                            "significance",
                            "wfo",
                            "ugc",
                        ]
                        existing_cols = [c for c in display_cols if c in history_df.columns]
                        if existing_cols:
                            st.dataframe(
                                history_df[existing_cols],
                                width="stretch",
                                height=220,
                                hide_index=True,
                            )
                    else:
                        st.markdown("**Heat Advisory Snapshot (past 12 months):** None")

            for name in enabled:
                if name in {"Heat risk", "Hurricane / wind exposure"}:
                    continue
                st.info(f"**{name}** is enabled — data integration will be wired in next.")
