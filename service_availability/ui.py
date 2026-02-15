
"""Service availability UI for FCC broadband coverage and delivery locations.

Displays interactive maps with FCC BDC broadband coverage data and delivery locations.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
import traceback
from urllib.parse import quote

import folium
import pandas as pd
import streamlit as st
from streamlit_folium import st_folium

from locations.logic import _get_loc_by_label
from .providers import (
    download_fcc_bdc_file,
    fetch_delivery_locations_live,
    fetch_grocery_locations_live,
    fetch_fcc_bdc_as_of_dates,
    fetch_fcc_bdc_availability_list,
    fetch_pharmacy_locations_live,
    load_google_maps_api_key,
    load_fcc_credentials,
    load_gpkg_features_for_radius_cached,
    preview_gpkg_layers,
    run_gpkg_overlay_for_address,
    test_fcc_credentials,
    unzip_fcc_bdc_file,
)


def _safe_float(value: object) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_speed(value: object) -> str:
    speed = _safe_float(value)
    if speed is None:
        return "—"
    return f"{speed:,.0f} Mbps"


def _filter_by_radius(items: list[dict], radius_miles: float) -> tuple[list[dict], int]:
    if not items:
        return [], 0
    filtered = []
    removed = 0
    for item in items:
        distance = item.get("distance_miles")
        if distance is None:
            filtered.append(item)
            continue
        if float(distance) <= radius_miles:
            filtered.append(item)
        else:
            removed += 1
    return filtered, removed


def _build_provider_rows(providers: list[dict]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for provider in providers:
        rows.append(
            {
                "Provider": provider.get("providerName") or provider.get("provider_name"),
                "Technology": provider.get("technology") or provider.get("technology_type"),
                "Max Download": _format_speed(
                    provider.get("maxDownloadSpeed") or provider.get("max_download_mbps")
                ),
                "Max Upload": _format_speed(
                    provider.get("maxUploadSpeed") or provider.get("max_upload_mbps")
                ),
            }
        )
    return rows


def _add_map_legend(m: folium.Map, html: str) -> None:
    container = (
        '<div style="position: fixed; bottom: 30px; left: 20px; z-index: 9999; '
        'background: white; padding: 10px 12px; border-radius: 6px; '
        'box-shadow: 0 2px 6px rgba(0,0,0,0.3); font-size: 12px;">'
        f"{html}"
        "</div>"
    )
    m.get_root().html.add_child(folium.Element(container))


def _render_broadbandnow_link(house: dict) -> None:
    """Render a small button linking to BroadbandNow with house address pre-filled."""
    address = house.get("address") or ""
    lat = house.get("lat")
    lon = house.get("lon")
    
    if not address or lat is None or lon is None:
        return
    
    # Parse address components: "4005 Ancient Oak Ct, Annandale, VA 22003, USA"
    # Extract state, city, zip from address
    state_name = None
    city = None
    zip_code = None
    
    # State abbreviation to full name mapping
    STATE_NAMES = {
        "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
        "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia",
        "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
        "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
        "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
        "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New-Hampshire", "NJ": "New-Jersey",
        "NM": "New-Mexico", "NY": "New-York", "NC": "North-Carolina", "ND": "North-Dakota", "OH": "Ohio",
        "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode-Island", "SC": "South-Carolina",
        "SD": "South-Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
        "VA": "Virginia", "WA": "Washington", "WV": "West-Virginia", "WI": "Wisconsin", "WY": "Wyoming",
        "DC": "District-of-Columbia",
    }
    
    # Extract city, state, zip: ", Annandale, VA 22003"
    match = re.search(r",\s*([^,]+),\s*([A-Z]{2})\s+(\d{5})", address)
    if match:
        city = match.group(1).strip()
        state_abbr = match.group(2)
        zip_code = match.group(3)
        state_name = STATE_NAMES.get(state_abbr)
    
    if not state_name or not city:
        # Fallback: just show a generic link
        url = f"https://broadbandnow.com/?address={quote(address)}"
    else:
        # Build full BroadbandNow URL
        # Format: https://broadbandnow.com/Virginia/Annandale?lat=38.836954&long=-77.1687474&zip=22003&address=...
        encoded_address = quote(address)
        url = (
            f"https://broadbandnow.com/{state_name}/{city}"
            f"?lat={lat}&long={lon}&zip={zip_code}&address={encoded_address}"
        )
    
    # Render as a small styled HTML button that opens in new tab
    button_html = f'''
    <a href="{url}" target="_blank" rel="noopener noreferrer" style="
        display: inline-flex;
        align-items: center;
        gap: 6px;
        padding: 4px 10px;
        background: linear-gradient(135deg, #1976D2, #1565C0);
        color: white;
        text-decoration: none;
        border-radius: 4px;
        font-size: 12px;
        font-weight: 500;
        box-shadow: 0 1px 3px rgba(0,0,0,0.2);
        transition: all 0.2s ease;
    " onmouseover="this.style.background='linear-gradient(135deg, #1565C0, #0D47A1)'; this.style.boxShadow='0 2px 5px rgba(0,0,0,0.3)';" 
       onmouseout="this.style.background='linear-gradient(135deg, #1976D2, #1565C0)'; this.style.boxShadow='0 1px 3px rgba(0,0,0,0.2)';">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>
            <polyline points="15 3 21 3 21 9"></polyline>
            <line x1="10" y1="14" x2="21" y2="3"></line>
        </svg>
        Check on BroadbandNow
    </a>
    '''
    st.markdown(button_html, unsafe_allow_html=True)


def _tech_color(tech: str | None) -> str:
    value = (tech or "").lower()
    if "fiber" in value:
        return "#2E7D32"
    if "cable" in value:
        return "#F9A825"
    if "dsl" in value:
        return "#EF6C00"
    if "satellite" in value or "wireless" in value:
        return "#C62828"
    return "#546E7A"


def _simplify_coverage_features(
    features: list[dict],
    *,
    tolerance: float | None = 0.0001,
    min_features: int = 5000,
    center_lat: float | None = None,
    center_lon: float | None = None,
    radius_miles: float | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Dissolve + simplify features to keep the interactive map responsive.

    Tolerance uses degrees (~0.0001 ≈ 10-12m). Set to None to preserve exact boundaries.
    Returns simplified features and stats.
    """
    stats = {"raw_features": len(features), "simplified_features": 0}
    if len(features) < min_features:
        return features, stats
    try:
        import pyproj
        from shapely.geometry import shape, mapping
        from shapely.ops import transform, unary_union
    except Exception:
        return features, stats

    search_area = None
    if center_lat is not None and center_lon is not None and radius_miles is not None:
        to_3857 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
        house_point_m = transform(to_3857, shape({
            "type": "Point",
            "coordinates": [center_lon, center_lat],
        }))
        radius_meters = radius_miles * 1609.34
        search_area = house_point_m.buffer(radius_meters)

    tech_meta = {
        10: {"name": "DSL", "category": "Fixed", "medium": "Copper", "spectrum": "Wired"},
        40: {"name": "Cable", "category": "Fixed", "medium": "Cable", "spectrum": "Wired"},
        50: {"name": "Fiber", "category": "Fixed", "medium": "Fiber", "spectrum": "Wired"},
        60: {"name": "Satellite", "category": "Fixed", "medium": "Satellite", "spectrum": "Licensed"},
        70: {"name": "Fixed Wireless", "category": "Fixed", "medium": "Wireless", "spectrum": "Mixed"},
        71: {"name": "Licensed Fixed Wireless", "category": "Fixed", "medium": "Wireless", "spectrum": "Licensed"},
        72: {"name": "Unlicensed Fixed Wireless", "category": "Fixed", "medium": "Wireless", "spectrum": "Unlicensed"},
        300: {"name": "3G Mobile", "category": "Mobile", "medium": "Wireless", "spectrum": "Licensed"},
        400: {"name": "4G LTE", "category": "Mobile", "medium": "Wireless", "spectrum": "Licensed"},
        500: {"name": "5G NR", "category": "Mobile", "medium": "Wireless", "spectrum": "Licensed"},
        0: {"name": "Other", "category": "Unknown", "medium": "Unknown", "spectrum": "Unknown"},
    }
    grouped: dict[int, list] = {}
    for feature in features:
        props = feature.get("properties") or {}
        tech_value = props.get("technology") or props.get("technology_code") or 0
        try:
            tech_code = int(tech_value)
        except (TypeError, ValueError):
            tech_code = 0
        geom = feature.get("geometry")
        if not geom:
            continue
        try:
            geom_shape = shape(geom)
        except Exception:
            continue
        if geom_shape.is_empty:
            continue
        grouped.setdefault(tech_code, []).append(geom_shape)

    simplified_features: list[dict] = []
    for tech_code, geoms in grouped.items():
        if not geoms:
            continue
        merged = unary_union(geoms)
        if search_area is not None:
            try:
                to_3857 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
                to_4326 = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
                merged_m = transform(to_3857, merged)
                merged_m = merged_m.intersection(search_area)
                merged = transform(to_4326, merged_m)
            except Exception:
                pass
        if tolerance is not None and tolerance > 0:
            try:
                merged = merged.simplify(tolerance, preserve_topology=True)
            except Exception:
                pass
        meta = tech_meta.get(tech_code, tech_meta[0])
        simplified_features.append(
            {
                "type": "Feature",
                "properties": {
                    "technology": tech_code,
                    "tech_name": meta["name"],
                    "tech_category": meta["category"],
                    "tech_medium": meta["medium"],
                    "tech_spectrum": meta["spectrum"],
                    "aggregated": True,
                },
                "geometry": mapping(merged),
            }
        )

    if simplified_features:
        stats["simplified_features"] = len(simplified_features)
        return simplified_features, stats
    return features, stats


def _render_service_map(
    *,
    house: dict,
    layer: str,
    payload: dict,
    radius_miles: float = 10,
    tech_filter: set[int] | None = None,
    show_radius: bool = True,
) -> None:
    m = folium.Map(
        location=[house["lat"], house["lon"]],
        zoom_start=12,
        tiles="OpenStreetMap",
    )

    folium.Marker(
        location=[house["lat"], house["lon"]],
        popup=f"<b>House</b><br>{house.get('address', '')}",
        icon=folium.Icon(color="red", icon="home"),
    ).add_to(m)

    if show_radius:
        radius_meters = radius_miles * 1609.34
        try:
            import pyproj
            from shapely.geometry import mapping
            from shapely.ops import transform
            from shapely.geometry import Point

            to_3857 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
            to_4326 = pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform
            house_point_m = transform(to_3857, Point(house["lon"], house["lat"]))
            search_area = house_point_m.buffer(radius_meters)
            search_area_4326 = transform(to_4326, search_area)
            folium.GeoJson(
                {"type": "Feature", "geometry": mapping(search_area_4326)},
                name="Search Radius",
                style_function=lambda _: {
                    "color": "#1565C0",
                    "weight": 2,
                    "fillOpacity": 0,
                    "dashArray": "6,6",
                },
                tooltip=f"{radius_miles} mile search radius",
                control=False,
                show=True,
            ).add_to(m)
        except Exception:
            folium.Circle(
                location=[house["lat"], house["lon"]],
                radius=radius_meters,
                color="#1565C0",
                weight=2,
                fill=False,
                dash_array="6,6",
                tooltip=f"{radius_miles} mile search radius",
            ).add_to(m)

    if layer == "Broadband (FCC)":
        # Render clipped coverage features if available
        fcc_coverage = payload.get("fcc_coverage_geojson") or {}
        features = fcc_coverage.get("features") or []
        
        # FCC BDC technology codes mapping
        # See: https://us-fcc.app.box.com/v/bdc-data-downloads-output
        TECH_CODES = {
            10: ("DSL", "#EF6C00", "#FF9800"),
            40: ("Cable", "#F9A825", "#FFEB3B"),
            50: ("Fiber", "#2E7D32", "#4CAF50"),
            60: ("Satellite", "#C62828", "#EF5350"),
            70: ("Fixed Wireless", "#7B1FA2", "#9C27B0"),
            71: ("Licensed Fixed Wireless", "#7B1FA2", "#9C27B0"),
            72: ("Unlicensed Fixed Wireless", "#7B1FA2", "#AB47BC"),
            0: ("Other", "#546E7A", "#90A4AE"),
            # Mobile broadband codes
            300: ("3G Mobile", "#1565C0", "#42A5F5"),
            400: ("4G LTE", "#0D47A1", "#1976D2"),
            500: ("5G NR", "#00695C", "#00897B"),
        }
        
        if features:
            # Style function based on technology code
            def coverage_style(feature):
                props = feature.get("properties") or {}
                tech_code = props.get("technology")
                try:
                    tech_code = int(tech_code)
                except (TypeError, ValueError):
                    tech_code = 0
                tech_info = TECH_CODES.get(tech_code, TECH_CODES[0])
                return {"color": tech_info[1], "weight": 1, "fillColor": tech_info[2], "fillOpacity": 0.4}

            tooltip_fields: list[str] = []
            tooltip_aliases: list[str] = []
            candidate_fields = [
                ("brandname", "Provider"),
                ("technology", "Tech Code"),
                ("tech_name", "Technology"),
                ("tech_category", "Category"),
                ("tech_medium", "Medium"),
                ("tech_spectrum", "Spectrum"),
                ("mindown", "Min Down (Mbps)"),
                ("minup", "Min Up (Mbps)"),
            ]
            sample_props = (features[0].get("properties") or {}) if features else {}
            for field, alias in candidate_fields:
                if field in sample_props:
                    tooltip_fields.append(field)
                    tooltip_aliases.append(alias)

            tooltip = None
            if tooltip_fields:
                tooltip = folium.GeoJsonTooltip(
                    fields=tooltip_fields,
                    aliases=tooltip_aliases,
                    sticky=True,
                )

            # Build separate layers per tech code to expose distinct checkboxes
            by_tech: dict[int, list[dict]] = {}
            for feature in features:
                props = feature.get("properties") or {}
                tech_value = props.get("technology")
                try:
                    tech_code = int(tech_value)
                except (TypeError, ValueError):
                    tech_code = 0
                if tech_filter is not None and tech_code not in tech_filter:
                    continue
                by_tech.setdefault(tech_code, []).append(feature)

            for tech_code, tech_features in sorted(by_tech.items(), key=lambda item: item[0]):
                tech_info = TECH_CODES.get(tech_code, TECH_CODES[0])
                layer_name = f"{tech_info[0]} (Tech {tech_code})"
                folium.GeoJson(
                    {"type": "FeatureCollection", "features": tech_features},
                    name=layer_name,
                    style_function=coverage_style,
                    tooltip=tooltip,
                    control=True,
                    show=True,
                ).add_to(m)
        else:
            # Fallback: just show tech tier circle if no coverage features
            tech_tier = (payload.get("summary") or {}).get("tech_tier")
            color = _tech_color(tech_tier)
            folium.Circle(
                location=[house["lat"], house["lon"]],
                radius=250,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.2,
                tooltip=f"Broadband tier: {tech_tier or 'Unknown'}",
            ).add_to(m)

        legend_html = (
            "<div style='font-weight:600;margin-bottom:6px;'>Broadband Coverage</div>"
            "<div><span style='color:#2E7D32;'>●</span> Fiber (50)</div>"
            "<div><span style='color:#F9A825;'>●</span> Cable (40)</div>"
            "<div><span style='color:#EF6C00;'>●</span> DSL (10)</div>"
            "<div><span style='color:#7B1FA2;'>●</span> Fixed Wireless (70-72)</div>"
            "<div><span style='color:#1565C0;'>●</span> 3G Mobile (300)</div>"
            "<div><span style='color:#0D47A1;'>●</span> 4G LTE (400)</div>"
            "<div><span style='color:#00695C;'>●</span> 5G NR (500)</div>"
            "<div><span style='color:#546E7A;'>●</span> Other</div>"
        )
        _add_map_legend(m, legend_html)

    elif layer in {"Delivery Locations", "Grocery", "Pharmacy"}:
        items = payload.get("delivery_locations") or []
        if layer == "Grocery":
            items = payload.get("grocery_locations") or []
        if layer == "Pharmacy":
            items = payload.get("pharmacy_locations") or []

        grocery_pharmacy_color = "#6A1B9A"
        color_map = {
            "USPS": "#1E88E5",
            "UPS": "#6D4C41",
            "FedEx": "#8E24AA",
            "DHL": "#FDD835",
            "Amazon Locker": "#FB8C00",
            "Grocery": "#2E7D32",
            "Pharmacy": "#D81B60",
            "Other": "#546E7A",
        }

        group = folium.FeatureGroup(name=layer, show=True)
        for item in items:
            lat = item.get("lat")
            lon = item.get("lon")
            if lat is None or lon is None:
                continue
            category = item.get("carrier") or item.get("category") or "Other"
            color = color_map.get(category, color_map["Other"])
            label = item.get("name") or "Location"
            if layer == "Grocery" and item.get("has_pharmacy"):
                label = f"{label} (Pharmacy)"
                color = grocery_pharmacy_color
            tooltip = f"{category}: {label}"
            folium.CircleMarker(
                location=[lat, lon],
                radius=6,
                color=color,
                fill=True,
                fill_color=color,
                fill_opacity=0.85,
                tooltip=tooltip,
            ).add_to(group)
        group.add_to(m)

        legend_rows = [f"<div style='font-weight:600;margin-bottom:6px;'>{layer}</div>"]
        if layer == "Delivery Locations":
            for carrier in ["USPS", "UPS", "FedEx", "DHL", "Amazon Locker", "Other"]:
                legend_rows.append(
                    f"<div><span style='color:{color_map[carrier]};'>●</span> {carrier}</div>"
                )
        elif layer == "Grocery":
            legend_rows.append(
                f"<div><span style='color:{color_map['Grocery']};'>●</span> Grocery Store</div>"
            )
            legend_rows.append(
                f"<div><span style='color:{grocery_pharmacy_color};'>●</span> Grocery with Pharmacy</div>"
            )
        else:
            legend_rows.append(
                f"<div><span style='color:{color_map['Pharmacy']};'>●</span> Pharmacy</div>"
            )
        _add_map_legend(m, "".join(legend_rows))

    else:
        folium.Circle(
            location=[house["lat"], house["lon"]],
            radius=200,
            color="#90A4AE",
            fill=True,
            fill_color="#90A4AE",
            fill_opacity=0.15,
            tooltip="Utilities coverage pending",
        ).add_to(m)
        legend_html = (
            "<div style='font-weight:600;margin-bottom:6px;'>Utilities</div>"
            "<div>Service coverage will render here once utility data is wired.</div>"
        )
        _add_map_legend(m, legend_html)

    folium.LayerControl(collapsed=False).add_to(m)
    st_folium(m, width=900, height=450, returned_objects=[])


def _render_places_summary(
    *,
    label: str,
    meta: dict | None,
    total_results: int,
    trimmed: int,
    radius_miles: float,
) -> None:
    if not meta:
        return
    st.caption(
        _places_summary_text(
            label=label,
            meta=meta,
            total_results=total_results,
            trimmed=trimmed,
            radius_miles=radius_miles,
        )
    )


def _places_summary_text(
    *,
    label: str,
    meta: dict | None,
    total_results: int,
    trimmed: int,
    radius_miles: float,
) -> str:
    if not meta:
        return f"{label}: no data"
    status = meta.get("status")
    requests = meta.get("requests")
    if meta.get("sources"):
        statuses = [source.get("status") for source in meta.get("sources")]
        status = ",".join(sorted({s for s in statuses if s}))
        requests = sum(int(source.get("requests") or 0) for source in meta.get("sources"))
    status_label = status or "Unknown"
    request_label = f" · requests {requests}" if requests is not None else ""
    return (
        f"{label}: status {status_label} · results {total_results} · "
        f"filtered {trimmed} beyond {radius_miles} mi{request_label}"
    )


def _render_fcc_section(payload: dict) -> None:
    st.markdown("#### FCC Broadband Map (BDC)")
    st.caption(
        "Source: FCC Broadband Data Collection Public Data API. Production data should be "
        "batch-ingested (nightly) and cached per census block to avoid rate limits."
    )
    username, token = load_fcc_credentials()
    if username and token:
        st.success("FCC credentials loaded from AWS Secrets Manager.")
    else:
        st.warning(
            "FCC credentials missing. Add houseplanner/fcc_username and "
            "houseplanner/fcc_api_key to AWS Secrets Manager."
        )
    with st.expander("Test FCC credentials", expanded=False):
        if st.button("Run FCC connectivity test", key="fcc_test"):
            result = test_fcc_credentials()
            if result.get("ok"):
                data = result.get("data") or {}
                st.success("FCC credentials are valid.")
                dates = data.get("data") or []
                if dates:
                    st.caption(
                        f"Latest availability date: {dates[0].get('as_of_date', 'unknown')}"
                    )
                st.json(data)
            else:
                st.error(result.get("error") or "FCC credential test failed.")
    with st.expander("Data pipeline + authentication notes", expanded=False):
        st.markdown(
            """
- FCC BDC API requires **username** + **hash_value** headers (token).
- Token should be stored in AWS Secrets Manager as `houseplanner/fcc_api_key`.
- API rate limit: **10 calls/minute per endpoint**.
- Avoid per-user API calls in production; ingest fixed broadband data offline and serve internally.
"""
        )

    with st.expander("FCC BDC API test workflow", expanded=False):
        st.caption("Run BDC steps 1–4 using the FCC credentials configured above.")
        if "fcc_bdc" not in st.session_state:
            st.session_state["fcc_bdc"] = {
                "as_of_dates": None,
                "latest_date": None,
                "availability_list": None,
                "fixed_broadband_list": None,
                "download_result": None,
            }

        if st.button("Step 1: listAsOfDates", key="fcc_bdc_step1"):
            try:
                data = fetch_fcc_bdc_as_of_dates()
                st.session_state["fcc_bdc"]["as_of_dates"] = data
                dates = [
                    item.get("as_of_date")
                    for item in data.get("data", [])
                    if item.get("data_type") == "availability"
                ]
                st.session_state["fcc_bdc"]["latest_date"] = dates[-1] if dates else None
                st.success("Fetched listAsOfDates.")
            except Exception as exc:
                st.error(f"BDC listAsOfDates failed: {exc}")

        latest_date = st.session_state["fcc_bdc"].get("latest_date")
        if latest_date:
            st.caption(f"Latest availability date: {latest_date}")

        if st.button("Step 2: listAvailabilityData", key="fcc_bdc_step2"):
            if not latest_date:
                st.warning("Run Step 1 first to get an availability date.")
            else:
                try:
                    data = fetch_fcc_bdc_availability_list(as_of_date=latest_date)
                    st.session_state["fcc_bdc"]["availability_list"] = data
                    st.success("Fetched availability list.")
                except Exception as exc:
                    st.error(f"BDC availability list failed: {exc}")

        if st.button("Step 3: Fixed Broadband filter", key="fcc_bdc_step3"):
            if not latest_date:
                st.warning("Run Step 1 first to get an availability date.")
            else:
                try:
                    data = fetch_fcc_bdc_availability_list(
                        as_of_date=latest_date,
                        category="Provider",
                        technology_type="Fixed Broadband",
                    )
                    st.session_state["fcc_bdc"]["fixed_broadband_list"] = data
                    st.success("Fetched fixed broadband list.")
                except Exception as exc:
                    st.error(f"BDC fixed broadband list failed: {exc}")

        file_id_input = st.text_input(
            "Step 4: Download file_id", value="", placeholder="Enter file_id from Step 2/3"
        )
        file_type = st.selectbox("File type", [2, 1], format_func=lambda v: "GeoPackage" if v == 2 else "Shapefile")
        output_path = st.text_input(
            "Output path",
            value="/tmp/fcc_downloads/coverage.gpkg.zip",
        )
        if st.button("Step 4: downloadFile", key="fcc_bdc_step4"):
            if not file_id_input:
                st.warning("Provide a file_id from Step 2 or Step 3.")
            else:
                try:
                    result = download_fcc_bdc_file(
                        file_id=int(file_id_input),
                        file_type=file_type,
                        output_path=output_path,
                    )
                    st.session_state["fcc_bdc"]["download_result"] = result
                    if result.get("ok"):
                        st.success(
                            f"Downloaded to {result.get('output_path')} ({result.get('bytes')} bytes)"
                        )
                    else:
                        st.error(
                            f"Download failed ({result.get('status_code')}): {result.get('error')}"
                        )
                except Exception as exc:
                    st.error(f"BDC download failed: {exc}")

        if st.session_state["fcc_bdc"].get("as_of_dates"):
            st.markdown("**Step 1 output**")
            st.json(st.session_state["fcc_bdc"]["as_of_dates"])
        if st.session_state["fcc_bdc"].get("availability_list"):
            st.markdown("**Step 2 output**")
            st.json(st.session_state["fcc_bdc"]["availability_list"])
        if st.session_state["fcc_bdc"].get("fixed_broadband_list"):
            st.markdown("**Step 3 output**")
            st.json(st.session_state["fcc_bdc"]["fixed_broadband_list"])
        if st.session_state["fcc_bdc"].get("download_result"):
            st.markdown("**Step 4 output**")
            st.json(st.session_state["fcc_bdc"]["download_result"])

        st.divider()
        st.markdown("**GeoPackage utilities**")
        unzip_target = st.text_input(
            "Zip file to unzip",
            value=output_path,
            key="fcc_bdc_unzip_path",
        )
        unzip_dir = st.text_input(
            "Extract directory",
            value="/tmp/fcc_downloads/extracted",
            key="fcc_bdc_unzip_dir",
        )
        if st.button("Unzip download", key="fcc_bdc_unzip"):
            result = unzip_fcc_bdc_file(zip_path=unzip_target, extract_dir=unzip_dir)
            st.session_state["fcc_bdc"]["unzip_result"] = result
            if result.get("ok"):
                st.success(f"Extracted to {result.get('extract_dir')}")
            else:
                st.error(result.get("error") or "Unzip failed")

        gpkg_path = st.text_input(
            "GeoPackage path",
            value="/tmp/fcc_downloads/extracted/coverage.gpkg",
            key="fcc_bdc_gpkg_path",
        )
        if st.button("Preview GeoPackage layers", key="fcc_bdc_preview"):
            result = preview_gpkg_layers(gpkg_path=gpkg_path)
            st.session_state["fcc_bdc"]["gpkg_preview"] = result
            if result.get("ok"):
                st.success("GeoPackage preview loaded.")
            else:
                st.error(result.get("error") or "Preview failed")

        overlay_address = st.text_input(
            "Overlay address",
            value="4005 Ancient Oak Ct, Annandale, VA 22003",
            key="fcc_bdc_overlay_address",
        )
        if st.button("Run overlay check", key="fcc_bdc_overlay"):
            result = run_gpkg_overlay_for_address(
                gpkg_path=gpkg_path,
                address=overlay_address,
            )
            st.session_state["fcc_bdc"]["overlay_result"] = result
            if result.get("ok"):
                st.success("Overlay check completed.")
            else:
                st.error(result.get("error") or "Overlay check failed")

        st.divider()
        st.markdown("**End-to-end workflow status**")
        if st.button("Run full FCC BDC + overlay workflow", key="fcc_bdc_full"):
            progress = st.progress(0, text="Step 1/7: listAsOfDates")
            try:
                data = fetch_fcc_bdc_as_of_dates()
                st.session_state["fcc_bdc"]["as_of_dates"] = data
                dates = [
                    item.get("as_of_date")
                    for item in data.get("data", [])
                    if item.get("data_type") == "availability"
                ]
                latest_date = dates[-1] if dates else None
                st.session_state["fcc_bdc"]["latest_date"] = latest_date
                progress.progress(14, text="Step 2/7: listAvailabilityData")
                if not latest_date:
                    raise RuntimeError("No availability dates returned.")
                availability = fetch_fcc_bdc_availability_list(as_of_date=latest_date)
                st.session_state["fcc_bdc"]["availability_list"] = availability
                progress.progress(28, text="Step 3/7: Fixed Broadband filter")
                fixed = fetch_fcc_bdc_availability_list(
                    as_of_date=latest_date,
                    category="Provider",
                    technology_type="Fixed Broadband",
                )
                st.session_state["fcc_bdc"]["fixed_broadband_list"] = fixed
                progress.progress(42, text="Step 4/7: Download coverage file")
                selected_file_id = None
                if file_id_input:
                    selected_file_id = int(file_id_input)
                else:
                    for item in fixed.get("data", []) if fixed else []:
                        if item.get("file_id"):
                            selected_file_id = int(item["file_id"])
                            break
                if not selected_file_id:
                    raise RuntimeError("No file_id available for download.")
                download_result = download_fcc_bdc_file(
                    file_id=selected_file_id,
                    file_type=file_type,
                    output_path=output_path,
                )
                st.session_state["fcc_bdc"]["download_result"] = download_result
                if not download_result.get("ok"):
                    raise RuntimeError(
                        f"Download failed ({download_result.get('status_code')}): {download_result.get('error')}"
                    )
                progress.progress(57, text="Step 5/7: Unzip download")
                unzip_result = unzip_fcc_bdc_file(zip_path=unzip_target, extract_dir=unzip_dir)
                st.session_state["fcc_bdc"]["unzip_result"] = unzip_result
                if not unzip_result.get("ok"):
                    raise RuntimeError(unzip_result.get("error") or "Unzip failed")
                progress.progress(71, text="Step 6/7: Preview GeoPackage layers")
                gpkg_preview = preview_gpkg_layers(gpkg_path=gpkg_path)
                st.session_state["fcc_bdc"]["gpkg_preview"] = gpkg_preview
                if not gpkg_preview.get("ok"):
                    raise RuntimeError(gpkg_preview.get("error") or "Preview failed")
                progress.progress(86, text="Step 7/7: Overlay check")
                overlay_result = run_gpkg_overlay_for_address(
                    gpkg_path=gpkg_path,
                    address=overlay_address,
                )
                st.session_state["fcc_bdc"]["overlay_result"] = overlay_result
                if not overlay_result.get("ok"):
                    raise RuntimeError(overlay_result.get("error") or "Overlay check failed")
                progress.progress(100, text="Workflow completed successfully.")
                st.success("FCC BDC + overlay workflow completed.")
            except Exception as exc:
                st.error(f"Workflow failed: {exc}")

        if st.session_state["fcc_bdc"].get("unzip_result"):
            st.markdown("**Unzip result**")
            st.json(st.session_state["fcc_bdc"]["unzip_result"])
        if st.session_state["fcc_bdc"].get("gpkg_preview"):
            st.markdown("**GeoPackage preview**")
            st.json(st.session_state["fcc_bdc"]["gpkg_preview"])
        if st.session_state["fcc_bdc"].get("overlay_result"):
            st.markdown("**Overlay result**")
            st.json(st.session_state["fcc_bdc"]["overlay_result"])

    if not payload:
        st.info("FCC broadband data has not been fetched yet for this location.")
        return

    block_fips = payload.get("block_fips") or payload.get("blockFips")
    if block_fips:
        st.caption(f"Census Block FIPS: {block_fips}")

    summary = payload.get("summary") or {}
    if summary:
        st.markdown("#### Service Snapshot")
        cols = st.columns(4)
        cols[0].metric("Technology Tier", summary.get("tech_tier") or "—")
        cols[1].metric("Service Score", summary.get("score") or "—")
        cols[2].metric("Max Download", _format_speed(summary.get("max_download_mbps")))
        cols[3].metric("Max Upload", _format_speed(summary.get("max_upload_mbps")))
        if summary.get("best_provider"):
            st.caption(f"Top Provider: **{summary.get('best_provider')}**")

    providers = payload.get("serviceProviders") or payload.get("providers") or []
    if providers:
        st.markdown("#### Provider Details")
        st.dataframe(
            pd.DataFrame(_build_provider_rows(providers)),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info("No FCC provider list available yet.")


def render_service_availability() -> None:
    with st.expander(
        "Service Availability",
        expanded=st.session_state.get("service_availability_expanded", False),
    ):
        locations = st.session_state.get("map_data", {}).get("locations", [])
        house = _get_loc_by_label(locations, "House")

        if not house:
            st.warning("Add a location labeled **House** to enable service availability.")
            return

        if "service_availability" not in st.session_state:
            st.session_state["service_availability"] = {}
        if "service_delivery_locations" not in st.session_state:
            st.session_state["service_delivery_locations"] = []
        if "service_grocery_locations" not in st.session_state:
            st.session_state["service_grocery_locations"] = []
        if "service_pharmacy_locations" not in st.session_state:
            st.session_state["service_pharmacy_locations"] = []

        # Search radius slider (shared across all tabs)
        radius_miles = st.slider(
            "Search radius (miles)",
            min_value=1,
            max_value=50,
            value=10,
            step=1,
            key="service_availability_radius_miles",
        )
        st.caption(
            "Radius is passed to Google Places for nearby searches. "
            "Each tab notes how the radius is applied."
        )

        # Create tabs for service layers
        tab_broadband, tab_delivery, tab_grocery, tab_pharmacy, tab_utilities = st.tabs(
            [
                "📡 Broadband (FCC)",
                "📦 Delivery Locations",
                "🛒 Grocery",
                "💊 Pharmacy",
                "⚡ Utilities (Planned)",
            ]
        )

        # --- Broadband (FCC) Tab ---
        with tab_broadband:
            payload = st.session_state.get("service_availability") or {}
            
            # BroadbandNow external link button
            _render_broadbandnow_link(house)

            if not payload:
                if st.button("Fetch FCC broadband availability", key="fcc_fetch"):
                    logging.basicConfig(level=logging.INFO)
                    logger = logging.getLogger("fcc_bdc_workflow")
                    progress = st.progress(0, text="Step 1/7: listAsOfDates")
                    tmp_dir = None
                    with st.status("Fetching FCC broadband availability...", expanded=True) as status:
                        try:
                            tmp_dir = tempfile.mkdtemp(prefix="fcc_bdc_")
                            zip_path = f"{tmp_dir}/coverage.gpkg.zip"
                            extract_dir = f"{tmp_dir}/extracted"
                            logger.info(f"Using tmp_dir: {tmp_dir}")
    
                            # Determine state FIPS from house address
                            # Virginia = 51, use address to extract state
                            house_address = house.get("address") or ""
                            state_fips = None
                            # Common state FIPS codes for matching
                            STATE_FIPS = {
                                "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06",
                                "CO": "08", "CT": "09", "DE": "10", "FL": "12", "GA": "13",
                                "HI": "15", "ID": "16", "IL": "17", "IN": "18", "IA": "19",
                                "KS": "20", "KY": "21", "LA": "22", "ME": "23", "MD": "24",
                                "MA": "25", "MI": "26", "MN": "27", "MS": "28", "MO": "29",
                                "MT": "30", "NE": "31", "NV": "32", "NH": "33", "NJ": "34",
                                "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
                                "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45",
                                "SD": "46", "TN": "47", "TX": "48", "UT": "49", "VT": "50",
                                "VA": "51", "WA": "53", "WV": "54", "WI": "55", "WY": "56",
                                "DC": "11", "PR": "72", "VI": "78", "GU": "66", "AS": "60",
                            }
                            # Extract state from address (e.g., "..., VA 22003")
                            state_match = re.search(r",\s*([A-Z]{2})\s+\d{5}", house_address)
                            if state_match:
                                state_abbr = state_match.group(1)
                                state_fips = STATE_FIPS.get(state_abbr)
                            st.write(f"📍 House state: {state_abbr if state_match else 'Unknown'} (FIPS: {state_fips or 'N/A'})")
                            logger.info(f"House state: {state_abbr if state_match else 'Unknown'}, FIPS: {state_fips}")
    
                            data = fetch_fcc_bdc_as_of_dates()
                            st.session_state.setdefault("fcc_bdc", {})
                            st.session_state["fcc_bdc"]["as_of_dates"] = data
                            dates = [
                                item.get("as_of_date")
                                for item in data.get("data", [])
                                if item.get("data_type") == "availability"
                            ]
                            latest_date = dates[-1] if dates else None
                            st.session_state["fcc_bdc"]["latest_date"] = latest_date
                            st.write(f"✅ Step 1: Found {len(dates)} availability dates. Latest: {latest_date}")
                            logger.info(f"Step 1: dates={dates}, latest={latest_date}")
                            progress.progress(14, text="Step 2/7: listAvailabilityData")
                            if not latest_date:
                                raise RuntimeError("No availability dates returned.")
                            availability = fetch_fcc_bdc_availability_list(as_of_date=latest_date)
                            st.session_state["fcc_bdc"]["availability_list"] = availability
                            avail_count = len(availability.get("data", []))
                            st.write(f"✅ Step 2: Found {avail_count} availability files")
                            logger.info(f"Step 2: {avail_count} files in availability list")
                            progress.progress(28, text="Step 3/7: Fixed Broadband filter")
                            fixed = fetch_fcc_bdc_availability_list(
                                as_of_date=latest_date,
                                category="Provider",
                                technology_type="Fixed Broadband",
                            )
                            st.session_state["fcc_bdc"]["fixed_broadband_list"] = fixed
                            fixed_count = len(fixed.get("data", []))
                            st.write(f"✅ Step 3: Found {fixed_count} fixed broadband files")
                            logger.info(f"Step 3: {fixed_count} fixed broadband files")
                            progress.progress(42, text="Step 4/7: downloadFile")
    
                            # Filter files by state FIPS code - collect all matches
                            # Prioritize by technology type (most useful first)
                            TECH_PRIORITY = {
                                50: 1,   # Fiber - highest priority
                                40: 2,   # Cable
                                10: 3,   # DSL/Copper  
                                70: 4,   # Fixed Wireless
                                71: 4,   # Licensed Fixed Wireless
                                72: 4,   # Unlicensed Fixed Wireless
                            }
                            MIN_RECORD_COUNT = 100  # Skip tiny files
                            
                            all_state_files = []
                            if state_fips:
                                for item in fixed.get("data", []) if fixed else []:
                                    fname = item.get("file_name") or ""
                                    if f"bdc_{state_fips}_" in fname or f"_{state_fips}_" in fname:
                                        if item.get("file_id"):
                                            try:
                                                record_count = int(item.get("record_count") or 0)
                                            except (TypeError, ValueError):
                                                record_count = 0
                                            try:
                                                tech_code = int(item.get("technology_code") or 0)
                                            except (TypeError, ValueError):
                                                tech_code = 0
                                            all_state_files.append({
                                                "file_id": int(item["file_id"]),
                                                "file_name": fname,
                                                "source": "fixed_broadband",
                                                "record_count": record_count,
                                                "technology_code": tech_code,
                                                "provider_name": item.get("provider_name", ""),
                                                "priority": TECH_PRIORITY.get(tech_code, 99),
                                            })
                                # Also try availability list
                                for item in availability.get("data", []) if availability else []:
                                    fname = item.get("file_name") or ""
                                    if f"bdc_{state_fips}_" in fname or f"_{state_fips}_" in fname:
                                        if item.get("file_id"):
                                            fid = int(item["file_id"])
                                            # Avoid duplicates
                                            if not any(f["file_id"] == fid for f in all_state_files):
                                                try:
                                                    record_count = int(item.get("record_count") or 0)
                                                except (TypeError, ValueError):
                                                    record_count = 0
                                                try:
                                                    tech_code = int(item.get("technology_code") or 0)
                                                except (TypeError, ValueError):
                                                    tech_code = 0
                                                all_state_files.append({
                                                    "file_id": fid,
                                                    "file_name": fname,
                                                    "source": "availability",
                                                    "record_count": record_count,
                                                    "technology_code": tech_code,
                                                    "provider_name": item.get("provider_name", ""),
                                                    "priority": TECH_PRIORITY.get(tech_code, 99),
                                                })
                            
                            st.write(f"📋 Found {len(all_state_files)} total files for state {state_fips}")
                            
                            # Filter: skip tiny files, prioritize by technology
                            state_matched_files = [f for f in all_state_files if int(f["record_count"] or 0) >= MIN_RECORD_COUNT]
                            state_matched_files.sort(key=lambda x: (x["priority"], -int(x["record_count"] or 0)))
                            
                            skipped_count = len(all_state_files) - len(state_matched_files)
                            if skipped_count > 0:
                                st.write(f"  ⏭️ Skipped {skipped_count} files with <{MIN_RECORD_COUNT} records")
                            
                            # Show technology breakdown
                            tech_counts = {}
                            for f in state_matched_files:
                                tc = int(f["technology_code"] or 0)
                                tech_counts[tc] = tech_counts.get(tc, 0) + 1
                            tech_labels = {10: "DSL", 40: "Cable", 50: "Fiber", 70: "Fixed Wireless", 71: "Lic Wireless", 72: "Unlic Wireless"}
                            st.write(f"  📊 By tech: {', '.join(f'{tech_labels.get(k, k)}:{v}' for k, v in sorted(tech_counts.items()))}")
                            
                            # Fallback: first files if no state match
                            if not state_matched_files:
                                for item in fixed.get("data", []) if fixed else []:
                                    if item.get("file_id"):
                                        state_matched_files.append({
                                            "file_id": int(item["file_id"]),
                                            "file_name": item.get("file_name"),
                                            "source": "fallback",
                                            "record_count": item.get("record_count") or 0,
                                            "technology_code": item.get("technology_code") or 0,
                                            "priority": 99,
                                        })
                                        if len(state_matched_files) >= 5:
                                            break
                                st.warning(f"⚠️ No state match found, using fallback files")
                            if not state_matched_files:
                                raise RuntimeError("No file_id available from fixed broadband list.")
                            st.write(f"✅ Will download {len(state_matched_files)} files (sorted by: Fiber > Cable > DSL > Wireless)")
                            logger.info(f"Step 4: {len(state_matched_files)} candidate files")
    
                            # Download ALL files for the state with rate limiting
                            # FCC API limit: 10 calls/minute - use sequential with 6 second delay
                            successful_downloads = []
                            failed_downloads = []
                            
                            st.write(f"📥 Step 4: Downloading {len(state_matched_files)} files (rate-limited: 10/min)...")
                            download_progress = st.progress(0, text="Starting downloads...")
                            
                            for idx, file_info in enumerate(state_matched_files):
                                fid = file_info["file_id"]
                                fname = file_info["file_name"]
                                out_path = f"{tmp_dir}/downloads/{fid}.gpkg.zip"
                                os.makedirs(f"{tmp_dir}/downloads", exist_ok=True)
                                
                                # Update progress
                                pct = int((idx / len(state_matched_files)) * 100)
                                download_progress.progress(pct, text=f"Downloading {idx+1}/{len(state_matched_files)}: {fname[:50]}...")
                                
                                result = download_fcc_bdc_file(
                                    file_id=fid,
                                    file_type=2,
                                    output_path=out_path,
                                )
                                
                                if result.get("ok"):
                                    successful_downloads.append({
                                        "file_info": file_info,
                                        "result": result,
                                        "output_path": out_path
                                    })
                                    st.write(f"✅ {idx+1}. Downloaded: {fname[:60]} ({result.get('bytes', 0):,} bytes)")
                                else:
                                    failed_downloads.append({
                                        "file_info": file_info,
                                        "result": result
                                    })
                                    status_code = result.get("status_code", "?")
                                    if status_code != 503:
                                        st.write(f"❌ {idx+1}. Failed ({status_code}): {fname[:60]}")
                                
                                # Rate limit: ~6 seconds between requests to stay under 10/min
                                if idx < len(state_matched_files) - 1:
                                    time.sleep(6)
                            
                            download_progress.progress(100, text="Downloads complete")
                            st.write(f"📊 Downloaded {len(successful_downloads)} of {len(state_matched_files)} files ({len(failed_downloads)} failed/503)")
                            logger.info(f"Step 4: {len(successful_downloads)} successful, {len(failed_downloads)} failed")
                            
                            if not successful_downloads:
                                raise RuntimeError("All download attempts failed (likely all 503 errors).")
                            
                            progress.progress(50, text="Step 5/7: Extract all GeoPackages")
                            
                            # Extract all successful downloads
                            gpkg_entries = []  # List of {file_id, gpkg_path} for cached processing
                            for dl in successful_downloads:
                                out_path = dl["output_path"]
                                extract_subdir = f"{extract_dir}/{dl['file_info']['file_id']}"
                                unzip_result = unzip_fcc_bdc_file(
                                    zip_path=out_path,
                                    extract_dir=extract_subdir,
                                )
                                if unzip_result.get("ok"):
                                    for f in unzip_result.get("files") or []:
                                        if f.endswith(".gpkg"):
                                            gpkg_entries.append({
                                                "file_id": dl["file_info"]["file_id"],
                                                "gpkg_path": f,
                                            })
                            
                            st.write(f"✅ Step 5: Extracted {len(gpkg_entries)} GeoPackage files")
                            logger.info(f"Step 5: {len(gpkg_entries)} gpkg files extracted")
                            
                            if not gpkg_entries:
                                raise RuntimeError("No .gpkg files found in downloaded archives")
    
                            progress.progress(65, text="Step 6/7: Preview layers")
                            
                            # Preview first file to show columns
                            preview = preview_gpkg_layers(gpkg_path=gpkg_entries[0]["gpkg_path"])
                            st.session_state["fcc_bdc"]["gpkg_preview"] = preview
                            if preview.get("ok"):
                                for layer_info in preview.get("layers") or []:
                                    st.write(f"  📋 Columns: {layer_info.get('columns')}")
                            
                            progress.progress(80, text="Step 7/7: Load & merge all features")
                            
                            # Load features from GeoPackage files and merge in parallel
                            # Using ThreadPoolExecutor instead of ProcessPoolExecutor to avoid
                            # pickling issues with Streamlit's hot-reloading
                            from concurrent.futures import ThreadPoolExecutor, as_completed
                            import threading
                            import queue
    
                            all_features = []
                            total_original = 0
                            total_clipped = 0
    
                            cpu_count = os.cpu_count() or 2
                            worker_count = min(4, max(1, cpu_count - 1))  # Limit threads to avoid memory pressure
                            st.write(f"⚙️ Using {worker_count} workers for GeoPackage processing")
                            merge_progress = st.progress(0, text="Processing GeoPackages...")
                            status_area = st.empty()
    
                            results_queue: queue.Queue[dict] = queue.Queue()
    
                            def _run_background_merge():
                                futures = []
                                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                                    for entry in gpkg_entries:
                                        futures.append(
                                            executor.submit(
                                                load_gpkg_features_for_radius_cached,
                                                gpkg_path=entry["gpkg_path"],
                                                file_id=entry["file_id"],
                                                lat=house.get("lat"),
                                                lon=house.get("lon"),
                                                radius_miles=radius_miles,
                                            )
                                        )
    
                                    for future in as_completed(futures):
                                        try:
                                            results_queue.put(future.result())
                                        except Exception as exc:
                                            results_queue.put({"ok": False, "error": str(exc)})
    
                            # Launch background thread
                            thread = threading.Thread(target=_run_background_merge, daemon=True)
                            thread.start()
    
                            completed = 0
                            total_files = len(gpkg_entries)
                            max_wait_seconds = 600  # 10 minute timeout for entire merge operation
                            start_time = time.time()
                            
                            while completed < total_files:
                                # Check for timeout
                                elapsed = time.time() - start_time
                                if elapsed > max_wait_seconds:
                                    logger.error(f"GeoPackage merge timed out after {elapsed:.0f}s")
                                    raise RuntimeError(f"GeoPackage processing timed out after {int(elapsed)}s")
                                
                                # Check if thread is still alive - if it crashed, stop waiting
                                if not thread.is_alive() and results_queue.empty():
                                    logger.error("Background merge thread terminated unexpectedly")
                                    raise RuntimeError("GeoPackage processing thread crashed")
                                
                                try:
                                    result = results_queue.get(timeout=0.5)
                                except queue.Empty:
                                    continue
    
                                completed += 1
                                pct = int((completed / total_files) * 100) if total_files else 100
                                merge_progress.progress(
                                    pct,
                                    text=f"Processed {completed}/{total_files} GeoPackages...",
                                )
    
                                if not result.get("ok"):
                                    logger.error(f"GeoPackage processing failed: {result.get('error')}")
                                    continue
    
                                geojson = result.get("geojson") or {}
                                all_features.extend(geojson.get("features") or [])
                                for layer_stat in result.get("layers") or []:
                                    total_original += layer_stat.get("feature_count", 0)
                                    total_clipped += layer_stat.get("clipped_count", 0)
    
                                status_area.markdown(
                                    f"**Merged features so far:** {len(all_features):,}"
                                )
    
                            # Wait for thread to finish gracefully
                            thread.join(timeout=5.0)
                            merge_progress.progress(100, text="GeoPackage processing complete")
                            
                            st.write(f"✅ Step 7: Merged {len(all_features)} features from {len(gpkg_entries)} files")
                            st.write(f"  📊 {total_clipped} of {total_original} features within {radius_miles} mile radius")
                            logger.info(f"Step 7: {len(all_features)} total features merged")
                            
                            simplified_features, simplify_stats = _simplify_coverage_features(
                                all_features,
                                center_lat=house.get("lat"),
                                center_lon=house.get("lon"),
                                radius_miles=radius_miles,
                            )

                            # Store merged GeoJSON in session state
                            st.session_state["service_availability"] = {
                                "fcc_coverage_geojson": {
                                    "type": "FeatureCollection",
                                    "features": simplified_features,
                                },
                                "fcc_coverage_stats": {
                                    "files_downloaded": len(successful_downloads),
                                    "files_failed": len(failed_downloads),
                                    "total_features": len(all_features),
                                    "original_features": total_original,
                                    "clipped_features": total_clipped,
                                    "simplified_features": simplify_stats.get("simplified_features"),
                                    "raw_features": simplify_stats.get("raw_features"),
                                },
                                "radius_miles": radius_miles,
                                "coverage_simplified": simplify_stats.get("simplified_features", 0) > 0,
                            }
                            
                            progress.progress(100, text="Workflow completed successfully.")
                            status.update(label=f"FCC BDC: {len(all_features)} features from {len(successful_downloads)} files", state="complete")
                        except Exception as exc:
                            logger.error(f"FCC workflow failed: {exc}\n{traceback.format_exc()}")
                            status.update(label="FCC broadband fetch failed", state="error")
                            st.error(f"FCC broadband fetch failed: {exc}")
                        finally:
                        # Cleanup tmp directory
                            if tmp_dir:
                                logger.info(f"Cleaning up tmp_dir: {tmp_dir}")
                                try:
                                    shutil.rmtree(tmp_dir, ignore_errors=True)
                                except Exception:
                                    pass
            
            # Always refresh payload to get latest data from session state
            payload = st.session_state.get("service_availability") or {}
            
            # Render map and section for broadband tab
            # Map should appear regardless of whether data was just fetched or already exists
            map_payload_broadband = dict(payload) if payload else {}
            map_payload_broadband["delivery_locations"] = []
            tech_filter = None
            fcc_features = (map_payload_broadband.get("fcc_coverage_geojson") or {}).get("features") or []
            if fcc_features:
                tech_options = sorted({
                    int((feature.get("properties") or {}).get("technology") or 0)
                    for feature in fcc_features
                })
                tech_labels = {
                    10: "DSL",
                    40: "Cable",
                    50: "Fiber",
                    60: "Satellite",
                    70: "Fixed Wireless",
                    71: "Licensed Fixed Wireless",
                    72: "Unlicensed Fixed Wireless",
                    300: "3G Mobile",
                    400: "4G LTE",
                    500: "5G NR",
                    0: "Other",
                }
                default_tech: list[int] = []
                with st.expander("Legend details", expanded=False):
                    st.markdown(
                        """
**Legend layers** (FCC technology codes):
- **Fiber (50)**: Fiber-to-the-premises or similar high-capacity fiber service.
- **Cable (40)**: Cable broadband.
- **DSL (10)**: Copper/DSL broadband.
- **Satellite (60)**: Satellite broadband coverage.
- **Fixed Wireless (70-72)**: Fixed wireless (licensed/unlicensed).
- **Mobile (300/400/500)**: Mobile broadband (3G/4G/5G NR).
- **Other (0)**: Uncategorized or unknown technology.
"""
                    )
                allow_multi = st.checkbox(
                    "Allow multiple overlay layers (may impact performance)",
                    value=False,
                    key="fcc_overlay_multi_toggle",
                )
                if allow_multi:
                    selected = st.multiselect(
                        "Overlay technologies",
                        options=tech_options,
                        default=default_tech,
                        format_func=lambda value: f"{tech_labels.get(value, 'Tech')} ({value})",
                        key="fcc_overlay_tech_filter",
                    )
                    st.caption("Select fewer layers to keep the interactive map responsive.")
                else:
                    none_label = "None (select a layer)"
                    selected_value = st.selectbox(
                        "Overlay technology",
                        options=[none_label] + tech_options,
                        index=0,
                        format_func=lambda value: (
                            none_label
                            if value == none_label
                            else f"{tech_labels.get(value, 'Tech')} ({value})"
                        ),
                        key="fcc_overlay_tech_single",
                    )
                    selected = [] if selected_value == none_label else [selected_value]
                    st.caption("Select one layer at a time for the most stable map rendering.")
                tech_filter = {int(value) for value in selected} if selected else set()
            _render_service_map(
                house=house,
                layer="Broadband (FCC)",
                payload=map_payload_broadband,
                radius_miles=radius_miles,
                tech_filter=tech_filter,
            )
            _render_fcc_section(payload)

        # --- Delivery Locations Tab ---
        with tab_delivery:
            delivery_locations = st.session_state.get("service_delivery_locations") or []
            delivery_meta = st.session_state.get("service_delivery_meta") or {}
            st.caption("Delivery search uses Google Places nearby results for the selected radius.")
            delivery_status = st.empty()
            delivery_progress = st.empty()
            if delivery_meta:
                delivery_status.caption(
                    _places_summary_text(
                        label="Delivery",
                        meta=delivery_meta,
                        total_results=len(delivery_locations),
                        trimmed=0,
                        radius_miles=radius_miles,
                    )
                )
            delivery_fetch = st.button(
                "Fetch delivery locations",
                key="service_delivery_fetch",
                help="Run UPS/USPS/FedEx/DHL/Amazon Locker search for this address.",
            )
            if delivery_fetch:
                api_key = load_google_maps_api_key()
                if not api_key:
                    st.error(
                        "Google Maps API key missing. Add the AWS Secrets Manager secret "
                        "'houseplanner/google_maps_api_key' to enable delivery locations."
                    )
                else:
                    progress = st.progress(0, text="Fetching delivery locations...")
                    delivery_status.caption("Delivery: running search...")
                    progress_area = delivery_progress.container()
                    progress_bar = progress_area.progress(0, text="Preparing delivery queries...")

                    def _delivery_progress(message: str, percent: int) -> None:
                        progress_bar.progress(percent, text=message)
                        delivery_status.caption(message)

                    with st.status("Fetching delivery locations...", expanded=False) as status:
                        try:
                            delivery_payload = fetch_delivery_locations_live(
                                api_key=api_key,
                                lat=house.get("lat"),
                                lon=house.get("lon"),
                                radius_meters=int(radius_miles * 1609.34),
                                progress_cb=_delivery_progress,
                            )
                            delivery_locations = delivery_payload.get("results") or []
                            delivery_meta = delivery_payload.get("meta") or {}
                            st.session_state["service_delivery_locations"] = delivery_locations
                            st.session_state["service_delivery_meta"] = delivery_meta
                            progress.progress(100, text="Delivery locations loaded")
                            delivery_status.caption(
                                _places_summary_text(
                                    label="Delivery",
                                    meta=delivery_meta,
                                    total_results=len(delivery_locations),
                                    trimmed=delivery_meta.get("filtered_out") or 0,
                                    radius_miles=radius_miles,
                                )
                            )
                            delivery_progress.empty()
                            status.update(label="Delivery locations loaded", state="complete")
                        except Exception as exc:
                            progress.progress(100, text="Delivery fetch failed")
                            delivery_status.caption("Delivery: fetch failed")
                            delivery_progress.empty()
                            status.update(label="Delivery fetch failed", state="error")
                            st.error(f"Delivery locations fetch failed: {exc}")

            # Render map and section for delivery tab
            map_payload_delivery = {}
            delivery_locations, delivery_trimmed = _filter_by_radius(
                delivery_locations, radius_miles
            )
            map_payload_delivery["delivery_locations"] = delivery_locations
            _render_service_map(
                house=house,
                layer="Delivery Locations",
                payload=map_payload_delivery,
                radius_miles=radius_miles,
                show_radius=False,
            )

            if delivery_locations:
                st.markdown("#### Delivery Locations")
                _render_places_summary(
                    label="Delivery",
                    meta=delivery_meta,
                    total_results=len(delivery_locations),
                    trimmed=delivery_trimmed,
                    radius_miles=radius_miles,
                )
                rows = []
                for item in delivery_locations:
                    rows.append(
                        {
                            "Carrier": item.get("carrier"),
                            "Name": item.get("name"),
                            "Distance (mi)": (
                                f"{item.get('distance_miles'):.2f}"
                                if item.get("distance_miles") is not None
                                else "—"
                            ),
                            "Rating": item.get("rating"),
                            "Open Now": item.get("open_now"),
                            "Vicinity": item.get("vicinity"),
                        }
                    )
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            else:
                st.info("Click **Fetch delivery locations** to run the carrier search.")
                if delivery_meta:
                    with st.expander("Delivery API diagnostics", expanded=False):
                        st.json(delivery_meta)

        with tab_grocery:
            grocery_locations = st.session_state.get("service_grocery_locations") or []
            grocery_meta = st.session_state.get("service_grocery_meta") or {}
            st.caption("Grocery search uses Google Places nearby results for the selected radius.")
            grocery_status = st.empty()
            grocery_progress = st.empty()
            if grocery_meta:
                grocery_status.caption(
                    _places_summary_text(
                        label="Grocery",
                        meta=grocery_meta,
                        total_results=len(grocery_locations),
                        trimmed=grocery_meta.get("filtered_out") or 0,
                        radius_miles=radius_miles,
                    )
                )
            grocery_fetch = st.button(
                "Fetch grocery locations",
                key="service_grocery_fetch",
                help="Search for grocery stores and flag which ones include a pharmacy.",
            )
            if grocery_fetch:
                api_key = load_google_maps_api_key()
                if not api_key:
                    st.error(
                        "Google Maps API key missing. Add the AWS Secrets Manager secret "
                        "'houseplanner/google_maps_api_key' to enable grocery locations."
                    )
                else:
                    progress = st.progress(0, text="Fetching grocery locations...")
                    grocery_status.caption("Grocery: running search...")
                    progress_area = grocery_progress.container()
                    progress_bar = progress_area.progress(0, text="Preparing grocery queries...")

                    def _grocery_progress(message: str, percent: int) -> None:
                        progress_bar.progress(percent, text=message)
                        grocery_status.caption(message)

                    with st.status("Fetching grocery locations...", expanded=False) as status:
                        try:
                            grocery_payload = fetch_grocery_locations_live(
                                api_key=api_key,
                                lat=house.get("lat"),
                                lon=house.get("lon"),
                                radius_meters=int(radius_miles * 1609.34),
                                progress_cb=_grocery_progress,
                            )
                            grocery_locations = grocery_payload.get("results") or []
                            grocery_meta = grocery_payload.get("meta") or {}
                            st.session_state["service_grocery_locations"] = grocery_locations
                            st.session_state["service_grocery_meta"] = grocery_meta
                            progress.progress(100, text="Grocery locations loaded")
                            grocery_status.caption(
                                _places_summary_text(
                                    label="Grocery",
                                    meta=grocery_meta,
                                    total_results=len(grocery_locations),
                                    trimmed=grocery_meta.get("filtered_out") or 0,
                                    radius_miles=radius_miles,
                                )
                            )
                            grocery_progress.empty()
                            status.update(label="Grocery locations loaded", state="complete")
                        except Exception as exc:
                            progress.progress(100, text="Grocery fetch failed")
                            grocery_status.caption("Grocery: fetch failed")
                            grocery_progress.empty()
                            status.update(label="Grocery fetch failed", state="error")
                            st.error(f"Grocery locations fetch failed: {exc}")

            map_payload_grocery = {"grocery_locations": grocery_locations}
            grocery_locations, grocery_trimmed = _filter_by_radius(
                grocery_locations, radius_miles
            )
            map_payload_grocery["grocery_locations"] = grocery_locations
            _render_service_map(
                house=house,
                layer="Grocery",
                payload=map_payload_grocery,
                radius_miles=radius_miles,
                show_radius=False,
            )

            if grocery_locations:
                st.markdown("#### Grocery Locations")
                _render_places_summary(
                    label="Grocery",
                    meta=grocery_meta,
                    total_results=len(grocery_locations),
                    trimmed=grocery_trimmed,
                    radius_miles=radius_miles,
                )
                rows = []
                for item in grocery_locations:
                    rows.append(
                        {
                            "Name": item.get("name"),
                            "Has Pharmacy": "Yes" if item.get("has_pharmacy") else "No",
                            "Distance (mi)": (
                                f"{item.get('distance_miles'):.2f}"
                                if item.get("distance_miles") is not None
                                else "—"
                            ),
                            "Rating": item.get("rating"),
                            "Open Now": item.get("open_now"),
                            "Vicinity": item.get("vicinity"),
                        }
                    )
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            else:
                st.info("Click **Fetch grocery locations** to run the grocery search.")
                if grocery_meta:
                    with st.expander("Grocery API diagnostics", expanded=False):
                        st.json(grocery_meta)

        with tab_pharmacy:
            pharmacy_locations = st.session_state.get("service_pharmacy_locations") or []
            pharmacy_meta = st.session_state.get("service_pharmacy_meta") or {}
            st.caption("Pharmacy search uses Google Places nearby results for the selected radius.")
            pharmacy_status = st.empty()
            pharmacy_progress = st.empty()
            if pharmacy_meta:
                pharmacy_status.caption(
                    _places_summary_text(
                        label="Pharmacy",
                        meta=pharmacy_meta,
                        total_results=len(pharmacy_locations),
                        trimmed=pharmacy_meta.get("filtered_out") or 0,
                        radius_miles=radius_miles,
                    )
                )
            pharmacy_fetch = st.button(
                "Fetch pharmacy locations",
                key="service_pharmacy_fetch",
                help="Search for standalone pharmacy facilities near this address.",
            )
            if pharmacy_fetch:
                api_key = load_google_maps_api_key()
                if not api_key:
                    st.error(
                        "Google Maps API key missing. Add the AWS Secrets Manager secret "
                        "'houseplanner/google_maps_api_key' to enable pharmacy locations."
                    )
                else:
                    progress = st.progress(0, text="Fetching pharmacy locations...")
                    pharmacy_status.caption("Pharmacy: running search...")
                    progress_area = pharmacy_progress.container()
                    progress_bar = progress_area.progress(0, text="Preparing pharmacy queries...")

                    def _pharmacy_progress(message: str, percent: int) -> None:
                        progress_bar.progress(percent, text=message)
                        pharmacy_status.caption(message)

                    with st.status("Fetching pharmacy locations...", expanded=False) as status:
                        try:
                            pharmacy_payload = fetch_pharmacy_locations_live(
                                api_key=api_key,
                                lat=house.get("lat"),
                                lon=house.get("lon"),
                                radius_meters=int(radius_miles * 1609.34),
                                progress_cb=_pharmacy_progress,
                            )
                            pharmacy_locations = pharmacy_payload.get("results") or []
                            pharmacy_meta = pharmacy_payload.get("meta") or {}
                            st.session_state["service_pharmacy_locations"] = pharmacy_locations
                            st.session_state["service_pharmacy_meta"] = pharmacy_meta
                            progress.progress(100, text="Pharmacy locations loaded")
                            pharmacy_status.caption(
                                _places_summary_text(
                                    label="Pharmacy",
                                    meta=pharmacy_meta,
                                    total_results=len(pharmacy_locations),
                                    trimmed=pharmacy_meta.get("filtered_out") or 0,
                                    radius_miles=radius_miles,
                                )
                            )
                            pharmacy_progress.empty()
                            status.update(label="Pharmacy locations loaded", state="complete")
                        except Exception as exc:
                            progress.progress(100, text="Pharmacy fetch failed")
                            pharmacy_status.caption("Pharmacy: fetch failed")
                            pharmacy_progress.empty()
                            status.update(label="Pharmacy fetch failed", state="error")
                            st.error(f"Pharmacy locations fetch failed: {exc}")

            pharmacy_locations, pharmacy_trimmed = _filter_by_radius(
                pharmacy_locations, radius_miles
            )
            map_payload_pharmacy = {"pharmacy_locations": pharmacy_locations}
            _render_service_map(
                house=house,
                layer="Pharmacy",
                payload=map_payload_pharmacy,
                radius_miles=radius_miles,
                show_radius=False,
            )

            if pharmacy_locations:
                st.markdown("#### Pharmacy Locations")
                _render_places_summary(
                    label="Pharmacy",
                    meta=pharmacy_meta,
                    total_results=len(pharmacy_locations),
                    trimmed=pharmacy_trimmed,
                    radius_miles=radius_miles,
                )
                rows = []
                for item in pharmacy_locations:
                    rows.append(
                        {
                            "Name": item.get("name"),
                            "Distance (mi)": (
                                f"{item.get('distance_miles'):.2f}"
                                if item.get("distance_miles") is not None
                                else "—"
                            ),
                            "Rating": item.get("rating"),
                            "Open Now": item.get("open_now"),
                            "Vicinity": item.get("vicinity"),
                        }
                    )
                st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
            else:
                st.info("Click **Fetch pharmacy locations** to run the pharmacy search.")
                if pharmacy_meta:
                    with st.expander("Pharmacy API diagnostics", expanded=False):
                        st.json(pharmacy_meta)

        # --- Utilities Tab ---
        with tab_utilities:
            # Render map for utilities tab
            map_payload_utilities = {}
            map_payload_utilities["delivery_locations"] = []
            _render_service_map(
                house=house,
                layer="Utilities (Planned)",
                payload=map_payload_utilities,
                radius_miles=radius_miles,
                show_radius=False,
            )
            st.info("Utility service tiers (power, water, gas) are planned for this layer.")
