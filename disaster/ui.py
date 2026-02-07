import streamlit as st
import folium
from streamlit_folium import st_folium
import math
import pyproj
from pathlib import Path
from shapely.geometry import Point, shape
from shapely.ops import transform
from collections import defaultdict
import pandas as pd
from branca.element import MacroElement
from jinja2 import Template

from locations.logic import _get_loc_by_label

from .doorprofit import (
    fetch_crime,
    fetch_offenders,
    fetch_usage,
    crime_incidents_to_features,
    offenders_to_features,
)

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
    fetch_fema_repetitive_loss_block_groups,
    repetitive_loss_total_style,
    repetitive_loss_unmitigated_style,
    REPETITIVE_LOSS_TOTAL_BUCKETS,
    REPETITIVE_LOSS_UNMITIGATED_BUCKETS,
)

from .wildfire import (
    fetch_mtbs_kmz,
    extract_geometry_kml,
    parse_kml_geometries,
)

from .heat import (
    fetch_heatrisk_point,
    fetch_nws_heat_alerts,
    fetch_historical_heat_events,
)

from .wind import fetch_wind_assessment
from .wind import fetch_wind_layers
from .earthquake import fetch_usgs_qfaults, earthquake_fault_style
from .watersheds import (
    fetch_usgs_huc12_watersheds,
    watershed_style,
    WATERSHED_HUTYPE_STYLES,
)
from .superfund import (
    fetch_superfund_polygons,
    fetch_superfund_cimc_points,
    superfund_polygon_style,
    superfund_point_style,
    SUPERFUND_STATUS_STYLES,
)
from .ui_styles import TORNADO_MAG_STYLES, WIND_SWATH_STYLES


def _safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


CONTENT_DIR = Path(__file__).with_name("content")
LEGEND_DIR = Path(__file__).with_name("legends")


def _load_template(path: Path, **kwargs):
    text = path.read_text(encoding="utf-8")
    return text.format(**kwargs) if kwargs else text


def load_content(filename: str, **kwargs):
    return _load_template(CONTENT_DIR / filename, **kwargs)


def load_legend(filename: str, **kwargs):
    return _load_template(LEGEND_DIR / filename, **kwargs)


def add_leaflet_legend_control(
    m: folium.Map,
    *,
    html: str,
    position: str = "bottomleft",
    container_style: str = "",
) -> None:
    """Add a legend using Leaflet's control system (robust in iframes).

    Using `position: fixed` inside the folium HTML root can be overridden by
    iframe/container CSS (streamlit-folium). A proper `L.control` is reliably
    placed by Leaflet itself.
    """

    safe_html = html.replace("`", "\\`")
    control_style = container_style.replace("`", "\\`")

    tpl = Template(
        """
        {% macro script(this, kwargs) %}
        (function() {
          var map = {{ this._parent.get_name() }};
          var legend = L.control({position: '{{ this.position }}'});
          legend.onAdd = function(map) {
            var div = L.DomUtil.create('div', 'dp-legend-control');
            div.innerHTML = `{{ this.html | safe }}`;
            if (`{{ this.style | safe }}`) {
              div.setAttribute('style', `{{ this.style | safe }}`);
            }
            // Don't steal scroll/drag.
            L.DomEvent.disableClickPropagation(div);
            L.DomEvent.disableScrollPropagation(div);
            return div;
          };
          legend.addTo(map);
        })();
        {% endmacro %}
        """
    )

    el = MacroElement()
    el._template = tpl
    el.html = safe_html
    el.position = position
    el.style = control_style
    m.add_child(el)


def _tornado_style(feature):
    magnitude = _safe_int(feature.get("properties", {}).get("mag"))
    if magnitude is not None:
        magnitude = max(0, min(5, magnitude))
    base = TORNADO_MAG_STYLES.get(magnitude)
    if base:
        return {k: v for k, v in base.items() if k in {"color", "weight", "dashArray"}}
    return {"color": "#283593", "weight": 2, "dashArray": "6,4"}


CRIME_TYPE_STYLES = {
    "Theft": {"color": "#0D47A1", "fill": "#1565C0"},
    "Assault": {"color": "#B71C1C", "fill": "#E53935"},
    "Burglary": {"color": "#4A148C", "fill": "#6A1B9A"},
    "Robbery": {"color": "#004D40", "fill": "#00897B"},
    "Vehicle Theft": {"color": "#E65100", "fill": "#FB8C00"},
    "Other": {"color": "#263238", "fill": "#546E7A"},
}


def _crime_marker_style(crime_type: str | None) -> dict:
    if not crime_type:
        return CRIME_TYPE_STYLES["Other"]
    ct = str(crime_type).strip().lower()
    if ct == "vehicle theft" or ct == "vehicle_theft":
        return CRIME_TYPE_STYLES["Vehicle Theft"]
    for k, v in CRIME_TYPE_STYLES.items():
        if k.lower() == ct:
            return v
    return CRIME_TYPE_STYLES["Other"]


OFFENDER_RISK_STYLES = {
    "HIGH": {"color": "#7F0000", "fill": "#C62828"},
    "MODERATE": {"color": "#E65100", "fill": "#EF6C00"},
    "LOW": {"color": "#1B5E20", "fill": "#2E7D32"},
    "NOT REPORTED": {"color": "#263238", "fill": "#546E7A"},
    "UNKNOWN": {"color": "#263238", "fill": "#546E7A"},
}


def _offender_marker_style(risk_level: str | None) -> dict:
    if not risk_level:
        return OFFENDER_RISK_STYLES["UNKNOWN"]
    key = str(risk_level).strip().upper()
    return OFFENDER_RISK_STYLES.get(key) or OFFENDER_RISK_STYLES["UNKNOWN"]


def _format_usage_datetime(value: str | None) -> str:
    if not value:
        return ""
    dt = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(dt):
        return str(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def _format_usage_percent(value: float | int | None) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _usage_key_value_table(title: str, rows: list[dict[str, str]]) -> None:
    if not rows:
        return
    st.markdown(f"**{title}**")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _usage_summary_table(title: str, payload: dict) -> None:
    if not payload:
        return
    row = {
        "Limit": payload.get("limit"),
        "Used": payload.get("used"),
        "Remaining": payload.get("remaining"),
        "Percent Used": _format_usage_percent(payload.get("percentage_used")),
        "Resets At": _format_usage_datetime(payload.get("resets_at")),
    }
    st.markdown(f"**{title}**")
    st.dataframe(pd.DataFrame([row]), width="stretch", hide_index=True)


def _render_doorprofit_usage(title: str) -> None:
    with st.expander(title, expanded=False):
        usage_error = st.session_state.get("doorprofit_usage_error")
        if usage_error:
            st.error(usage_error)
            return

        if "doorprofit_usage_json" not in st.session_state:
            try:
                st.session_state["doorprofit_usage_json"] = fetch_usage()
            except Exception as exc:
                st.session_state["doorprofit_usage_error"] = str(exc)
                st.error(str(exc))
                return

        payload = st.session_state.get("doorprofit_usage_json") or {}
        data = payload.get("data", {}) or {}

        plan = data.get("plan") or {}
        _usage_key_value_table(
            "Plan",
            [
                {"Metric": "Name", "Value": str(plan.get("name") or "")},
                {"Metric": "Type", "Value": str(plan.get("type") or "")},
                {"Metric": "Monthly Price", "Value": str(plan.get("price_monthly") or "")},
            ]
            if plan
            else [],
        )

        _usage_summary_table("Monthly usage", data.get("monthly") or {})
        _usage_summary_table("Daily usage", data.get("daily") or {})

        rate_limit = data.get("rate_limit") or {}
        _usage_key_value_table(
            "Rate limit",
            [
                {
                    "Metric": "Requests per second",
                    "Value": str(rate_limit.get("requests_per_second") or ""),
                }
            ]
            if rate_limit
            else [],
        )

        overage = data.get("overage") or {}
        _usage_key_value_table(
            "Overage",
            [
                {"Metric": "Enabled", "Value": "Yes" if overage.get("enabled") else "No"},
                {"Metric": "Calls", "Value": str(overage.get("calls") or "0")},
                {"Metric": "Amount due", "Value": str(overage.get("amount_due") or "")},
                {"Metric": "Rate per call", "Value": str(overage.get("rate_per_call") or "")},
            ]
            if overage
            else [],
        )

        account = data.get("account") or {}
        _usage_key_value_table(
            "Account",
            [
                {"Metric": "Status", "Value": str(account.get("status") or "")},
                {
                    "Metric": "Subscription status",
                    "Value": str(account.get("subscription_status") or ""),
                },
                {"Metric": "Blocked", "Value": "Yes" if account.get("blocked") else "No"},
            ]
            if account
            else [],
        )

        meta = payload.get("meta") or {}
        note = meta.get("note") or ""
        timestamp = _format_usage_datetime(meta.get("timestamp"))
        if note or timestamp:
            meta_parts = []
            if note:
                meta_parts.append(note)
            if timestamp:
                meta_parts.append(f"Timestamp: {timestamp}")
            st.caption(" • ".join(meta_parts))


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

            st.markdown(load_content("planned_layers.md"))

            c1, c2 = st.columns(2)

            with c1:
                st.checkbox("Flood zones (FEMA)", key="hz_flood")
                st.checkbox("Wildfire risk", key="hz_wildfire")
                st.checkbox("Historical disaster declarations", key="hz_disaster_history")
                st.checkbox("FEMA repetitive loss areas (advanced risk)", key="hz_fema_rl")
                st.checkbox("EPA Superfund sites (CERCLA)", key="hz_superfund")
                st.checkbox("Crime incidents (DoorProfit)", key="hz_crime")

            with c2:
                st.checkbox("Heat risk", key="hz_heat")
                st.checkbox("Earthquake fault proximity", key="hz_earthquake")
                st.checkbox("Wind exposure", key="hz_wind")
                st.checkbox("Watersheds (USGS HUC-12)", key="hz_watershed")
                st.checkbox("Registered sex offenders (DoorProfit)", key="hz_sex_offenders")

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

            if not st.session_state.get("hz_fema_rl"):
                st.session_state.pop("fema_rl_geojson", None)
                st.session_state.pop("fema_rl_radius_key", None)

            if not st.session_state.get("hz_earthquake"):
                st.session_state.pop("usgs_qfaults_geojson", None)
                st.session_state.pop("usgs_qfaults_radius_key", None)

            if not st.session_state.get("hz_watershed"):
                st.session_state.pop("usgs_huc12_geojson", None)
                st.session_state.pop("usgs_huc12_radius_key", None)
                st.session_state.pop("usgs_huc12_schema", None)

            if not st.session_state.get("hz_superfund"):
                st.session_state.pop("superfund_polygons_geojson", None)
                st.session_state.pop("superfund_points_geojson", None)
                st.session_state.pop("superfund_radius_key", None)

            if not st.session_state.get("hz_crime"):
                st.session_state.pop("doorprofit_crime_json", None)
                st.session_state.pop("doorprofit_crime_radius_key", None)

            if not st.session_state.get("hz_sex_offenders"):
                st.session_state.pop("doorprofit_offenders_json", None)
                st.session_state.pop("doorprofit_offenders_radius_key", None)

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
            track_features = []
            tornado_features = []
            repetitive_loss_total_features = []
            repetitive_loss_unmitigated_features = []
            earthquake_features = []
            watershed_features = []
            superfund_polygons = []
            superfund_points = []
            superfund_npl_points = []

            crime_incident_rows = []
            offender_rows = []

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
                    m.get_root().html.add_child(
                        folium.Element(load_legend("wildfire_legend.html"))
                    )

            # ---------------------------------------
            # FEMA Repetitive Loss Areas (Block Group)
            # ---------------------------------------
            if st.session_state.get("hz_fema_rl"):
                if (
                        "fema_rl_geojson" not in st.session_state
                        or st.session_state.get("fema_rl_radius_key") != bbox_key
                ):
                    st.session_state["fema_rl_geojson"] = (
                        fetch_fema_repetitive_loss_block_groups(bbox)
                    )
                    st.session_state["fema_rl_radius_key"] = bbox_key

                rl_geojson = st.session_state.get("fema_rl_geojson") or {}

                for feature in rl_geojson.get("features", []):
                    props = feature.get("properties")
                    geom = feature.get("geometry")

                    if not props or not geom:
                        continue

                    polygon_m = transform(project, shape(geom))

                    if polygon_m.intersects(search_area):
                        clipped_geom = polygon_m.intersection(search_area)

                        if not clipped_geom.is_empty:
                            base_feature = {
                                "type": "Feature",
                                "properties": props,
                                "geometry": transform(
                                    pyproj.Transformer.from_crs(
                                        "EPSG:3857", "EPSG:4326", always_xy=True
                                    ).transform,
                                    clipped_geom,
                                ).__geo_interface__,
                            }

                            total_count = props.get("any_rl") or 0
                            unmitigated_count = props.get("any_rl_unmitigated") or 0

                            if total_count > 0:
                                repetitive_loss_total_features.append(base_feature)

                            if unmitigated_count > 0:
                                repetitive_loss_unmitigated_features.append(base_feature)

                if repetitive_loss_total_features:
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": repetitive_loss_total_features,
                        },
                        name="FEMA repetitive loss (total, block group)",
                        style_function=repetitive_loss_total_style,
                        tooltip=folium.GeoJsonTooltip(
                            fields=["geoid_bg", "any_rl", "any_rl_unmitigated"],
                            aliases=[
                                "Block Group",
                                "Total RL (count)",
                                "Unmitigated RL (count)",
                            ],
                            sticky=True,
                        ),
                        control=True,
                        show=False,
                    ).add_to(m)

                if repetitive_loss_unmitigated_features:
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": repetitive_loss_unmitigated_features,
                        },
                        name="FEMA repetitive loss (unmitigated, block group)",
                        style_function=repetitive_loss_unmitigated_style,
                        tooltip=folium.GeoJsonTooltip(
                            fields=["geoid_bg", "any_rl", "any_rl_unmitigated"],
                            aliases=[
                                "Block Group",
                                "Total RL (count)",
                                "Unmitigated RL (count)",
                            ],
                            sticky=True,
                        ),
                        control=True,
                        show=False,
                    ).add_to(m)

                if repetitive_loss_total_features or repetitive_loss_unmitigated_features:
                    total_legend_items = "".join(
                        f"""
                        <div style='margin-bottom:4px;'>
                            <span style='display:inline-block;width:14px;height:14px;
                                background:{colors['fill']};
                                border:1px solid {colors['stroke']};
                                margin-right:6px;'></span>
                            {min_val}–{int(max_val) if max_val != float('inf') else '+'}
                        </div>
                        """
                        for min_val, max_val, colors in REPETITIVE_LOSS_TOTAL_BUCKETS
                    )

                    unmitigated_legend_items = "".join(
                        f"""
                        <div style='margin-bottom:4px;'>
                            <span style='display:inline-block;width:14px;height:14px;
                                background:{colors['fill']};
                                border:1px solid {colors['stroke']};
                                margin-right:6px;'></span>
                            {min_val}–{int(max_val) if max_val != float('inf') else '+'}
                        </div>
                        """
                        for min_val, max_val, colors in REPETITIVE_LOSS_UNMITIGATED_BUCKETS
                    )

                    m.get_root().html.add_child(
                        folium.Element(
                            load_legend(
                                "fema_repetitive_loss_legend.html",
                                total_legend_items=total_legend_items,
                                unmitigated_legend_items=unmitigated_legend_items,
                            )
                        )
                    )

            # ---------------------------------------
            # Earthquake fault lines (USGS Qfaults)
            # ---------------------------------------
            if st.session_state.get("hz_earthquake"):
                if (
                        "usgs_qfaults_geojson" not in st.session_state
                        or st.session_state.get("usgs_qfaults_radius_key") != bbox_key
                ):
                    st.session_state["usgs_qfaults_geojson"] = fetch_usgs_qfaults(bbox)
                    st.session_state["usgs_qfaults_radius_key"] = bbox_key

                qfaults_geojson = st.session_state.get("usgs_qfaults_geojson") or {}

                for feature in qfaults_geojson.get("features", []):
                    props = feature.get("properties")
                    geom = feature.get("geometry")

                    if not props or not geom:
                        continue

                    line_m = transform(project, shape(geom))

                    if line_m.intersects(search_area):
                        clipped_geom = line_m.intersection(search_area)

                        if not clipped_geom.is_empty:
                            earthquake_features.append({
                                "type": "Feature",
                                "properties": props,
                                "geometry": transform(
                                    pyproj.Transformer.from_crs(
                                        "EPSG:3857", "EPSG:4326", always_xy=True
                                    ).transform,
                                    clipped_geom,
                                ).__geo_interface__,
                            })

                if earthquake_features:
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": earthquake_features,
                        },
                        name="USGS Quaternary faults",
                        style_function=earthquake_fault_style,
                        tooltip=folium.GeoJsonTooltip(
                            fields=["fault_name", "section_name", "age", "slip_rate"],
                            aliases=["Fault", "Section", "Age", "Slip rate"],
                            sticky=True,
                        ),
                        control=True,
                        show=False,
                    ).add_to(m)

                    m.get_root().html.add_child(
                        folium.Element(load_legend("earthquake_legend.html"))
                    )

            # ---------------------------------------
            # Watersheds (USGS HUC-12)
            # ---------------------------------------
            if st.session_state.get("hz_watershed"):
                if (
                        "usgs_huc12_geojson" not in st.session_state
                        or st.session_state.get("usgs_huc12_radius_key") != bbox_key
                        or st.session_state.get("usgs_huc12_schema") != "huc12_v2"
                ):
                    st.session_state["usgs_huc12_geojson"] = fetch_usgs_huc12_watersheds(bbox)
                    st.session_state["usgs_huc12_radius_key"] = bbox_key
                    st.session_state["usgs_huc12_schema"] = "huc12_v2"

                huc12_geojson = st.session_state.get("usgs_huc12_geojson") or {}

                for feature in huc12_geojson.get("features", []):
                    props = feature.get("properties")
                    geom = feature.get("geometry")

                    if not props or not geom:
                        continue

                    polygon_m = transform(project, shape(geom))

                    if polygon_m.intersects(search_area):
                        clipped_geom = polygon_m.intersection(search_area)

                        if not clipped_geom.is_empty:
                            watershed_features.append({
                                "type": "Feature",
                                "properties": props,
                                "geometry": transform(
                                    pyproj.Transformer.from_crs(
                                        "EPSG:3857", "EPSG:4326", always_xy=True
                                    ).transform,
                                    clipped_geom,
                                ).__geo_interface__,
                            })

                if watershed_features:
                    tooltip_fields = ["huc12", "name", "areasqkm", "states"]
                    tooltip_aliases = ["HUC-12", "Watershed", "Area (sq km)", "States"]

                    if any(
                        feature.get("properties", {}).get("hutype")
                        for feature in watershed_features
                    ):
                        tooltip_fields.insert(2, "hutype")
                        tooltip_aliases.insert(2, "HU Type")

                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": watershed_features,
                        },
                        name="USGS HUC-12 watersheds",
                        style_function=watershed_style,
                        tooltip=folium.GeoJsonTooltip(
                            fields=tooltip_fields,
                            aliases=tooltip_aliases,
                            sticky=True,
                        ),
                        control=True,
                        show=False,
                    ).add_to(m)

                    watershed_legend_items = "".join(
                        f"""
                        <div style='margin-bottom:4px;'>
                            <span style='display:inline-block;width:14px;height:14px;
                                background:{style['fill']};
                                border:1px solid {style['stroke']};
                                margin-right:6px;'></span>
                            {style['label']}
                        </div>
                        """
                        for style in WATERSHED_HUTYPE_STYLES.values()
                    )

                    m.get_root().html.add_child(
                        folium.Element(
                            load_legend(
                                "watershed_legend.html",
                                watershed_legend_items=watershed_legend_items,
                            )
                        )
                    )

            # ---------------------------------------
            # EPA Superfund Sites (CERCLA)
            # ---------------------------------------
            if st.session_state.get("hz_superfund"):
                if (
                        "superfund_polygons_geojson" not in st.session_state
                        or st.session_state.get("superfund_radius_key") != bbox_key
                ):
                    st.session_state["superfund_polygons_geojson"] = fetch_superfund_polygons(bbox)
                    st.session_state["superfund_points_geojson"] = fetch_superfund_cimc_points(bbox)
                    st.session_state["superfund_radius_key"] = bbox_key

                polygons_geojson = st.session_state.get("superfund_polygons_geojson") or {}
                points_geojson = st.session_state.get("superfund_points_geojson") or {}

                for feature in polygons_geojson.get("features", []):
                    props = feature.get("properties")
                    geom = feature.get("geometry")

                    if not props or not geom:
                        continue

                    polygon_m = transform(project, shape(geom))

                    if polygon_m.intersects(search_area):
                        clipped_geom = polygon_m.intersection(search_area)

                        if not clipped_geom.is_empty:
                            superfund_polygons.append({
                                "type": "Feature",
                                "properties": props,
                                "geometry": transform(
                                    pyproj.Transformer.from_crs(
                                        "EPSG:3857", "EPSG:4326", always_xy=True
                                    ).transform,
                                    clipped_geom,
                                ).__geo_interface__,
                            })

                            centroid = clipped_geom.centroid
                            superfund_npl_points.append({
                                "type": "Feature",
                                "properties": {
                                    "SITE_NAME": props.get("SITE_NAME"),
                                    "EPA_ID": props.get("EPA_ID"),
                                    "NPL_STATUS_CODE": props.get("NPL_STATUS_CODE"),
                                    "STATE_CODE": props.get("STATE_CODE"),
                                    "COUNTY": props.get("COUNTY"),
                                    "CITY_NAME": props.get("CITY_NAME"),
                                },
                                "geometry": transform(
                                    pyproj.Transformer.from_crs(
                                        "EPSG:3857", "EPSG:4326", always_xy=True
                                    ).transform,
                                    centroid,
                                ).__geo_interface__,
                            })

                for feature in points_geojson.get("features", []):
                    props = feature.get("properties")
                    geom = feature.get("geometry")

                    if not props or not geom:
                        continue

                    point_m = transform(project, shape(geom))

                    if point_m.intersects(search_area):
                        superfund_points.append(feature)

                if superfund_polygons:
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": superfund_polygons,
                        },
                        name="Superfund NPL site boundaries",
                        style_function=superfund_polygon_style,
                        tooltip=folium.GeoJsonTooltip(
                            fields=["SITE_NAME", "EPA_ID", "NPL_STATUS_CODE", "STATE_CODE"],
                            aliases=["Site", "EPA ID", "NPL Status", "State"],
                            sticky=True,
                        ),
                        control=True,
                        show=False,
                    ).add_to(m)

                if superfund_points:
                    superfund_points_group = folium.FeatureGroup(
                        name="Superfund CIMC sites (points)",
                        show=False,
                    )

                    for feature in superfund_points:
                        props = feature.get("properties", {})
                        geom = feature.get("geometry") or {}
                        coords = geom.get("coordinates")

                        if not coords:
                            continue

                        style = superfund_point_style(feature)
                        tooltip_text = (
                            f"<b>{props.get('SF_SITE_NAME', 'Superfund Site')}</b><br>"
                            f"SF ID: {props.get('SF_SITE_ID', 'Unknown')}<br>"
                            f"Archived: {props.get('SF_ARCHIVED_IND', 'Unknown')}<br>"
                            f"State: {props.get('STATE_CODE', 'Unknown')}"
                        )

                        folium.CircleMarker(
                            location=[coords[1], coords[0]],
                            radius=style["radius"],
                            color=style["color"],
                            fill=True,
                            fill_color=style["fillColor"],
                            fill_opacity=style["fillOpacity"],
                            weight=style["weight"],
                            tooltip=folium.Tooltip(tooltip_text),
                        ).add_to(superfund_points_group)

                    superfund_points_group.add_to(m)

                if superfund_npl_points:
                    superfund_npl_group = folium.FeatureGroup(
                        name="Superfund NPL sites (centroids)",
                        show=False,
                    )

                    for feature in superfund_npl_points:
                        props = feature.get("properties", {})
                        geom = feature.get("geometry") or {}
                        coords = geom.get("coordinates")

                        if not coords:
                            continue

                        tooltip_text = (
                            f"<b>{props.get('SITE_NAME', 'NPL Site')}</b><br>"
                            f"EPA ID: {props.get('EPA_ID', 'Unknown')}<br>"
                            f"NPL Status: {props.get('NPL_STATUS_CODE', 'Unknown')}<br>"
                            f"State: {props.get('STATE_CODE', 'Unknown')}"
                        )

                        folium.CircleMarker(
                            location=[coords[1], coords[0]],
                            radius=4,
                            color="#5E35B1",
                            fill=True,
                            fill_color="#9575CD",
                            fill_opacity=0.8,
                            weight=1,
                            tooltip=folium.Tooltip(tooltip_text),
                        ).add_to(superfund_npl_group)

                    superfund_npl_group.add_to(m)

                if superfund_polygons or superfund_points or superfund_npl_points:
                    superfund_legend_items = "".join(
                        f"""
                        <div style='margin-bottom:4px;'>
                            <span style='display:inline-block;width:14px;height:14px;
                                background:{style['color']};
                                border:1px solid {style['color']};
                                margin-right:6px;'></span>
                            {style['label']}
                        </div>
                        """
                        for style in SUPERFUND_STATUS_STYLES.values()
                    )

                    m.get_root().html.add_child(
                        folium.Element(
                            load_legend(
                                "superfund_legend.html",
                                superfund_legend_items=superfund_legend_items,
                            )
                        )
                    )

            # ---------------------------------------
            # DoorProfit Crime Incidents (points)
            # ---------------------------------------
            if st.session_state.get("hz_crime"):
                if (
                    "doorprofit_crime_json" not in st.session_state
                    or st.session_state.get("doorprofit_crime_radius_key") != bbox_key
                ):
                    st.session_state["doorprofit_crime_json"] = fetch_crime(house.get("address") or "")
                    st.session_state["doorprofit_crime_radius_key"] = bbox_key

                crime_json = st.session_state.get("doorprofit_crime_json") or {}
                crime_features = crime_incidents_to_features(crime_json)

                # Clip to circular radius
                to_3857 = pyproj.Transformer.from_crs(
                    "EPSG:4326", "EPSG:3857", always_xy=True
                ).transform

                crimes_group = folium.FeatureGroup(
                    name=f"Crime incidents [{len(crime_features)}]",
                    show=False,
                )

                for f in crime_features:
                    props = f.get("properties", {}) or {}
                    coords = (f.get("geometry") or {}).get("coordinates") or []
                    if len(coords) < 2:
                        continue

                    lng, lat = coords[0], coords[1]
                    pt_m = transform(to_3857, Point(float(lng), float(lat)))
                    if not pt_m.intersects(search_area):
                        continue

                    ct = props.get("type")
                    style = _crime_marker_style(ct)
                    tooltip_text = (
                        f"<b>{props.get('type','Incident')}</b><br>"
                        f"Date: {props.get('date','')}<br>"
                        f"Distance (ft): {props.get('distance_feet','')}<br>"
                        f"{props.get('address','')}"
                    )
                    folium.CircleMarker(
                        location=[float(lat), float(lng)],
                        radius=5,
                        color=style["color"],
                        fill=True,
                        fill_color=style["fill"],
                        fill_opacity=0.85,
                        weight=1,
                        tooltip=folium.Tooltip(tooltip_text),
                    ).add_to(crimes_group)

                    crime_incident_rows.append(
                        {
                            "type": props.get("type"),
                            "date": props.get("date"),
                            "address": props.get("address"),
                            "distance_feet": props.get("distance_feet"),
                            "lat": float(lat),
                            "lng": float(lng),
                        }
                    )

                if crime_incident_rows:
                    crimes_group.add_to(m)
                    add_leaflet_legend_control(
                        m,
                        html=load_legend("crime_legend.html"),
                        position="bottomleft",
                        container_style=(
                            "background: white; padding: 10px 14px; border-radius: 6px; "
                            "box-shadow: 0 2px 6px rgba(0,0,0,0.3); font-size: 13px; "
                            "max-width: 260px; pointer-events: none;"
                        ),
                    )

            # ---------------------------------------
            # DoorProfit Registered Sex Offenders (points)
            # ---------------------------------------
            if st.session_state.get("hz_sex_offenders"):
                if (
                    "doorprofit_offenders_json" not in st.session_state
                    or st.session_state.get("doorprofit_offenders_radius_key") != bbox_key
                ):
                    st.session_state["doorprofit_offenders_json"] = fetch_offenders(house.get("address") or "")
                    st.session_state["doorprofit_offenders_radius_key"] = bbox_key

                offenders_json = st.session_state.get("doorprofit_offenders_json") or {}
                offender_features = offenders_to_features(offenders_json)

                to_3857 = pyproj.Transformer.from_crs(
                    "EPSG:4326", "EPSG:3857", always_xy=True
                ).transform

                offenders_group = folium.FeatureGroup(
                    name=f"Sex offenders [{len(offender_features)}]",
                    show=False,
                )

                for f in offender_features:
                    props = f.get("properties", {}) or {}
                    coords = (f.get("geometry") or {}).get("coordinates") or []
                    if len(coords) < 2:
                        continue
                    lng, lat = coords[0], coords[1]
                    pt_m = transform(to_3857, Point(float(lng), float(lat)))
                    if not pt_m.intersects(search_area):
                        continue

                    risk = props.get("risk_level")
                    style = _offender_marker_style(risk)

                    tooltip_text = (
                        f"<b>{props.get('name','Offender')}</b><br>"
                        f"Risk: {props.get('risk_level','Unknown')}<br>"
                        f"Distance (mi): {props.get('distance','')}<br>"
                        f"{props.get('address','')}, {props.get('city','') or ''} {props.get('state','') or ''} {props.get('zipcode','') or ''}"
                    )
                    folium.CircleMarker(
                        location=[float(lat), float(lng)],
                        radius=6,
                        color=style["color"],
                        fill=True,
                        fill_color=style["fill"],
                        fill_opacity=0.85,
                        weight=1,
                        tooltip=folium.Tooltip(tooltip_text),
                    ).add_to(offenders_group)

                    offender_rows.append(
                        {
                            "name": props.get("name"),
                            "risk_level": props.get("risk_level"),
                            "distance_mi": props.get("distance"),
                            "address": ", ".join(
                                [
                                    str(props.get("address") or "").strip(),
                                    str(props.get("city") or "").strip(),
                                    str(props.get("state") or "").strip(),
                                    str(props.get("zipcode") or "").strip(),
                                ]
                            ).replace(" ,", ",").strip(", "),
                            "gender": props.get("gender"),
                            "age": props.get("age"),
                            "dob": props.get("dob"),
                            "source_url": props.get("source_url"),
                            "lat": float(lat),
                            "lng": float(lng),
                        }
                    )

                if offender_rows:
                    offenders_group.add_to(m)
                    add_leaflet_legend_control(
                        m,
                        html=load_legend("sex_offender_legend.html"),
                        position="bottomleft",
                        container_style=(
                            "background: white; padding: 10px 14px; border-radius: 6px; "
                            "box-shadow: 0 2px 6px rgba(0,0,0,0.3); font-size: 13px; "
                            "max-width: 260px; pointer-events: none;"
                        ),
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
                    st.session_state["heatrisk_cache_key"] = heatrisk_key

                heatrisk_summary = st.session_state.get("heatrisk_point")
                heat_alerts = st.session_state.get("heatrisk_alerts")
                heat_history = st.session_state.get("heatrisk_history")



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

                # Hurricane wind swaths (polygons) - grouped by level
                for level, features in wind_layers["wind_swaths"].items():
                    if features:
                        swath_count = len(features)
                        swath_group_name = f"🌀 Wind Swaths ({level}) [{swath_count}]"
                        folium.GeoJson(
                            {
                                "type": "FeatureCollection",
                                "features": features,
                            },
                            name=swath_group_name,
                            style_function=lambda _, s=WIND_SWATH_STYLES[level]: s,
                            control=True,
                            show=False,
                        ).add_to(m)

                # Hurricane tracks (lines) - all in one group
                if wind_layers["tracks"]:
                    for feature in wind_layers["tracks"]:
                        props = feature.get("properties", {}) or {}
                        label = (
                            props.get("stormname")
                            or props.get("name")
                            or props.get("storm_id")
                            or "Hurricane"
                        )
                        advisory = props.get("advisory") or props.get("advisory_number") or props.get("advisoryNumber") or ""
                        dtg = props.get("datetime") or props.get("advisoryTime") or props.get("dtg") or ""
                        
                        enriched_feature = {
                            "type": "Feature",
                            "properties": {
                                **props,
                                "label": label,
                                "advisory": advisory,
                                "datetime": dtg,
                            },
                            "geometry": feature.get("geometry"),
                        }
                        track_features.append(enriched_feature)

                    track_count = len(track_features)
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": track_features,
                        },
                        name=f"🌀 Hurricane Tracks [{track_count}]",
                        style_function=lambda _: {
                            "color": "#6A1B9A",
                            "weight": 2.5,
                        },
                        tooltip=folium.GeoJsonTooltip(
                            fields=["label", "advisory", "datetime"],
                            aliases=["Storm", "Advisory", "Timestamp"],
                            sticky=True,
                        ),
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
                # Tornado paths (lines) - all in one group
                if wind_layers["tornado_paths"]:
                    for feature in wind_layers["tornado_paths"]:
                        props = feature.get("properties", {}) or {}
                        year = _safe_int(props.get("yr"))
                        month = _safe_int(props.get("mo"))
                        day = _safe_int(props.get("dy"))
                        magnitude = props.get("mag")
                        length = props.get("len")
                        
                        label_parts = [
                            part
                            for part in [
                                str(year) if year is not None else None,
                                (
                                    f"{month:02d}-{day:02d}"
                                    if month is not None and day is not None
                                    else None
                                ),
                                f"Mag {magnitude}" if magnitude is not None else None,
                            ]
                            if part
                        ]
                        label = "Tornado path" + (" • " + " ".join(label_parts) if label_parts else "")
                        
                        enriched_feature = {
                            "type": "Feature",
                            "properties": {
                                **props,
                                "label": label,
                                "year": year,
                                "magnitude": magnitude,
                                "path_length_mi": length,
                            },
                            "geometry": feature.get("geometry"),
                        }
                        tornado_features.append(enriched_feature)

                    tornado_count = len(tornado_features)
                    folium.GeoJson(
                        {
                            "type": "FeatureCollection",
                            "features": tornado_features,
                        },
                        name=f"🌪️ Tornado Paths [{tornado_count}]",
                        style_function=_tornado_style,
                        tooltip=folium.GeoJsonTooltip(
                            fields=["label", "year", "magnitude", "path_length_mi"],
                            aliases=["Event", "Year", "Magnitude", "Length (mi)"],
                            sticky=True,
                        ),
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
                            load_content(
                                "flood_zone.md",
                                title=zone_info["title"],
                                summary=zone_info["summary"],
                                insurance=zone_info["insurance"],
                                sfha="Yes" if house_sfha == "T" else "No",
                            ),
                            unsafe_allow_html=True,
                        )

            if st.session_state.get("hz_fema_rl"):
                if repetitive_loss_total_features or repetitive_loss_unmitigated_features:
                    total_rl = sum(
                        feature.get("properties", {}).get("any_rl") or 0
                        for feature in repetitive_loss_total_features
                    )
                    total_unmitigated = sum(
                        feature.get("properties", {}).get("any_rl_unmitigated") or 0
                        for feature in repetitive_loss_unmitigated_features
                    )

                    st.markdown(
                        load_content(
                            "fema_rl_present.md",
                            total_count=len(repetitive_loss_total_features),
                            unmitigated_count=len(repetitive_loss_unmitigated_features),
                            total_rl=total_rl,
                            total_unmitigated=total_unmitigated,
                        )
                    )
                else:
                    st.markdown(
                        load_content(
                            "fema_rl_empty.md",
                            radius_miles=radius_miles,
                        )
                    )

            if st.session_state.get("hz_earthquake"):
                if earthquake_features:
                    st.markdown(
                        load_content(
                            "earthquake_present.md",
                            fault_count=len(earthquake_features),
                        )
                    )
                else:
                    st.markdown(
                        load_content(
                            "earthquake_empty.md",
                            radius_miles=radius_miles,
                        )
                    )

            if st.session_state.get("hz_watershed"):
                if watershed_features:
                    hutype_counts = defaultdict(int)
                    for feature in watershed_features:
                        hutype = feature.get("properties", {}).get("hutype") or "Unknown"
                        hutype_counts[hutype] += 1

                    hutype_summary = "\n".join(
                        f"- **{WATERSHED_HUTYPE_STYLES.get(code, {'label': code}).get('label', code)}**: {count} polygon(s)"
                        for code, count in sorted(hutype_counts.items())
                    )

                    st.markdown(
                        load_content(
                            "watershed_present.md",
                            watershed_count=len(watershed_features),
                            hutype_summary=hutype_summary,
                        )
                    )
                else:
                    st.markdown(
                        load_content(
                            "watershed_empty.md",
                            radius_miles=radius_miles,
                        )
                    )

            if st.session_state.get("hz_superfund"):
                if superfund_polygons or superfund_points or superfund_npl_points:
                    status_counts = defaultdict(int)
                    for feature in superfund_points:
                        status = feature.get("properties", {}).get("SF_ARCHIVED_IND") or "Unknown"
                        status_counts[status] += 1

                    status_summary = "\n".join(
                        f"- **{SUPERFUND_STATUS_STYLES.get(code, {'label': code}).get('label', code)}**: {count} point(s)"
                        for code, count in sorted(status_counts.items())
                    )

                    st.markdown(
                        load_content(
                            "superfund_present.md",
                            npl_count=len(superfund_polygons),
                            point_count=len(superfund_points),
                            centroid_count=len(superfund_npl_points),
                            status_summary=status_summary,
                        )
                    )
                else:
                    st.markdown(
                        load_content(
                            "superfund_empty.md",
                            radius_miles=radius_miles,
                        )
                    )

            # ---------------------------------------
            # DoorProfit Tables
            # ---------------------------------------
            if st.session_state.get("hz_crime"):
                st.markdown("#### Crime incidents")
                if crime_incident_rows:
                    crime_df = pd.DataFrame(crime_incident_rows)
                    if "date" in crime_df.columns:
                        crime_df["date"] = pd.to_datetime(crime_df["date"], errors="coerce")
                        crime_df = crime_df.sort_values("date", ascending=False)
                        crime_df["date"] = crime_df["date"].dt.date.astype("string")
                    st.dataframe(crime_df, width="stretch", height=260, hide_index=True)
                else:
                    st.caption("No incidents returned within this search radius.")
                _render_doorprofit_usage("DoorProfit API usage (/v1/usage) — Crime")

            if st.session_state.get("hz_sex_offenders"):
                st.markdown("#### Registered sex offenders")
                if offender_rows:
                    offenders_df = pd.DataFrame(offender_rows)
                    # Group/Sort: highest risk first, then closest.
                    risk_order = {"HIGH": 0, "MODERATE": 1, "LOW": 2, "NOT REPORTED": 3}
                    offenders_df["risk_sort"] = (
                        offenders_df["risk_level"].astype(str).str.upper().map(risk_order).fillna(9)
                    )
                    if "distance_mi" in offenders_df.columns:
                        offenders_df["distance_mi"] = pd.to_numeric(offenders_df["distance_mi"], errors="coerce")
                    offenders_df = offenders_df.sort_values(["risk_sort", "distance_mi"], ascending=[True, True])
                    offenders_df.drop(columns=["risk_sort"], inplace=True, errors="ignore")
                    st.dataframe(offenders_df, width="stretch", height=260, hide_index=True)
                else:
                    st.caption("No offenders returned within this search radius.")
                _render_doorprofit_usage("DoorProfit API usage (/v1/usage) — Sex offenders")

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
                        load_content(
                            "wildfire_present.md",
                            fire_count=fire_count,
                            radius_miles=radius_miles,
                            most_recent_year=most_recent_year,
                        )
                    )
                else:
                    st.markdown(
                        load_content(
                            "wildfire_empty.md",
                            radius_miles=radius_miles,
                        )
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
                        load_content(
                            "disaster_history_present.md",
                            designated_area=designated_area,
                            state_abbrev=state_abbrev,
                            record_count=len(rows),
                        )
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
                        load_content(
                            "disaster_history_empty.md",
                            designated_area=designated_area or "Unknown",
                            state_abbrev=state_abbrev or "Unknown",
                        )
                    )

                    nearby_zones = set()

            if st.session_state.get("hz_wind"):
                if wind_assessment:
                    if wind_assessment.get("asce_available"):
                        asce = wind_assessment.get("asce") or {}
                        st.markdown(
                            load_content(
                                "wind_asce.md",
                                design_speed=asce.get("design_wind_speed_mph")
                                or "Not available",
                                risk_category=asce.get("risk_category", "II"),
                                standard=asce.get("standard", "7-16"),
                                hurricane_prone="Yes"
                                if asce.get("is_hurricane_prone")
                                else "No",
                            )
                        )
                    else:
                        st.markdown(
                            load_content(
                                "wind_screening.md",
                                wind_category=wind_assessment[
                                    "screening_wind_category"
                                ],
                                hurricane_force="Yes"
                                if wind_assessment["hurricane_force_winds"]
                                else "No",
                                risk_tier=wind_assessment["risk_tier"],
                                source=wind_assessment["source"],
                                note=wind_assessment.get("note", ""),
                            )
                        )
                else:
                    st.markdown(load_content("wind_no_data.md"))

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

                    swath_counts = {
                        level: len(features)
                        for level, features in swaths.items()
                        if isinstance(features, list)
                    }
                    track_count = len(wind_layers.get("tracks") or [])
                    tornado_layers = wind_layers.get("tornado_paths") or []
                    tornado_count = len(tornado_layers)
                    tornado_years = sorted(
                        {
                            _safe_int(feature.get("properties", {}).get("yr"))
                            for feature in tornado_layers
                        }
                    )
                    tornado_years = [year for year in tornado_years if year is not None]

                    if swath_counts or track_count or tornado_count:
                        swath_summary = ", ".join(
                            f"{level}: {count}" for level, count in swath_counts.items()
                        )
                        year_range = (
                            f"{tornado_years[0]}–{tornado_years[-1]}"
                            if tornado_years
                            else "unknown"
                        )
                        st.info(
                            "Wind layers returned — "
                            f"Swaths ({swath_summary or 'none'}), "
                            f"Hurricane tracks: {track_count}, "
                            f"Tornado tracks: {tornado_count} (years {year_range})"
                        )

                    swath_rows = []
                    for level, features in swaths.items():
                        for feature in features or []:
                            props = feature.get("properties", {}) or {}
                            swath_rows.append({
                                "wind_level": level,
                                "file_date": props.get("idp_filedate"),
                                "percentage": props.get("percentage"),
                            })

                    track_rows = []
                    for feature in track_features:
                        props = feature.get("properties", {}) or {}
                        track_rows.append({
                            "storm": props.get("label"),
                            "advisory": props.get("advisory"),
                            "datetime": props.get("datetime"),
                        })

                    tornado_rows = []
                    for feature in tornado_features:
                        props = feature.get("properties", {}) or {}
                        year = _safe_int(props.get("yr"))
                        month = _safe_int(props.get("mo"))
                        day = _safe_int(props.get("dy"))
                        date_value = None
                        if year and month and day:
                            date_value = f"{year:04d}-{month:02d}-{day:02d}"
                        tornado_rows.append({
                            "date": date_value,
                            "magnitude": props.get("mag"),
                            "path_length_mi": props.get("len"),
                        })

                    has_any_table = any([swath_rows, track_rows, tornado_rows])
                    if has_any_table:
                        st.markdown("#### Wind events")

                    if track_rows:
                        tracks_df = pd.DataFrame(track_rows)
                        tracks_df["datetime"] = pd.to_datetime(
                            tracks_df["datetime"], errors="coerce"
                        )
                        tracks_df = tracks_df.sort_values("datetime", ascending=False)
                        tracks_df["datetime"] = tracks_df["datetime"].dt.strftime(
                            "%Y-%m-%d %H:%M"
                        )
                        st.markdown("**Hurricane tracks**")
                        st.dataframe(tracks_df, width="stretch", hide_index=True)

                    if tornado_rows:
                        tornado_df = pd.DataFrame(tornado_rows)
                        tornado_df["date"] = pd.to_datetime(
                            tornado_df["date"], errors="coerce"
                        )
                        tornado_df = tornado_df.sort_values("date", ascending=False)
                        tornado_df["date"] = tornado_df["date"].dt.date.astype("string")
                        st.markdown("**Tornado paths**")
                        st.dataframe(tornado_df, width="stretch", hide_index=True)

                    if swath_rows:
                        swath_df = pd.DataFrame(swath_rows)
                        swath_df["file_date"] = pd.to_datetime(
                            swath_df["file_date"], errors="coerce"
                        )
                        swath_df = swath_df.sort_values("file_date", ascending=False)
                        swath_df["file_date"] = swath_df["file_date"].dt.date.astype("string")
                        st.markdown("**Hurricane wind swaths**")
                        st.dataframe(swath_df, width="stretch", hide_index=True)

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

            if st.session_state["hz_wind"]:
                enabled.append("Hurricane / wind exposure")

            if st.session_state["hz_heat"]:
                enabled.append("Heat risk")

            if st.session_state.get("hz_heat"):
                if heatrisk_summary:
                    st.markdown(
                        load_content(
                            "heat_present.md",
                            heat_label=heatrisk_summary.get("label", "Unknown"),
                        )
                    )
                else:
                    st.markdown(load_content("heat_empty.md"))

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
