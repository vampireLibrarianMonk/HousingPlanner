from __future__ import annotations

from pathlib import Path

import folium
import pandas as pd
import pyproj
import requests
from shapely.geometry import Point
from shapely.ops import transform
import streamlit as st
from streamlit_folium import st_folium
from branca.element import MacroElement
from jinja2 import Template

from locations.logic import _get_loc_by_label
from disaster.declarations import reverse_geocode_county_state

from .logic import (
    grade_range,
    match_schooldigger_to_places,
    clip_geojson_to_radius,
    normalize_name,
    fuzzy_match_score,
    build_greatschools_search_url,
    build_niche_url,
)
from .providers import (
    fetch_google_places_schools,
    fetch_schooldigger_schools,
    fetch_urban_institute_schools_by_state,
    fetch_nces_district_boundaries,
    load_google_maps_api_key,
    load_schooldigger_keys,
    parse_google_school,
    parse_schooldigger_school,
)

LEGEND_DIR = Path(__file__).with_name("legends")
STATE_FIPS = {
    "AL": "01",
    "AK": "02",
    "AZ": "04",
    "AR": "05",
    "CA": "06",
    "CO": "08",
    "CT": "09",
    "DE": "10",
    "DC": "11",
    "FL": "12",
    "GA": "13",
    "HI": "15",
    "ID": "16",
    "IL": "17",
    "IN": "18",
    "IA": "19",
    "KS": "20",
    "KY": "21",
    "LA": "22",
    "ME": "23",
    "MD": "24",
    "MA": "25",
    "MI": "26",
    "MN": "27",
    "MS": "28",
    "MO": "29",
    "MT": "30",
    "NE": "31",
    "NV": "32",
    "NH": "33",
    "NJ": "34",
    "NM": "35",
    "NY": "36",
    "NC": "37",
    "ND": "38",
    "OH": "39",
    "OK": "40",
    "OR": "41",
    "PA": "42",
    "RI": "44",
    "SC": "45",
    "SD": "46",
    "TN": "47",
    "TX": "48",
    "UT": "49",
    "VT": "50",
    "VA": "51",
    "WA": "53",
    "WV": "54",
    "WI": "55",
    "WY": "56",
}


def _load_legend(filename: str) -> str:
    return (LEGEND_DIR / filename).read_text(encoding="utf-8")


def add_leaflet_legend_control(
    m: folium.Map,
    *,
    html: str,
    position: str = "bottomleft",
    container_style: str = "",
) -> None:
    safe_html = html.replace("`", "\\`")
    control_style = container_style.replace("`", "\\`")

    tpl = Template(
        """
        {% macro script(this, kwargs) %}
        (function() {
          var map = {{ this._parent.get_name() }};
          var legend = L.control({position: '{{ this.position }}'});
          legend.onAdd = function(map) {
            var container = L.DomUtil.create('div', 'schools-legend-container');
            container.style.cssText = 'pointer-events: auto;';
            
            // Toggle button
            var toggleBtn = L.DomUtil.create('div', 'schools-legend-toggle', container);
            toggleBtn.innerHTML = '📋 Legend';
            toggleBtn.style.cssText = 'background: white; padding: 6px 10px; border-radius: 4px; ' +
              'box-shadow: 0 2px 6px rgba(0,0,0,0.3); cursor: pointer; font-size: 13px; ' +
              'font-weight: 600; user-select: none; margin-bottom: 4px; display: inline-block;';
            
            // Legend content (initially visible)
            var content = L.DomUtil.create('div', 'schools-legend-content', container);
            content.innerHTML = `{{ this.html | safe }}`;
            if (`{{ this.style | safe }}`) {
              content.setAttribute('style', `{{ this.style | safe }}`);
            }
            
            // Track visibility state
            var isVisible = true;
            
            // Toggle click handler
            toggleBtn.onclick = function(e) {
              e.stopPropagation();
              isVisible = !isVisible;
              content.style.display = isVisible ? 'block' : 'none';
              toggleBtn.innerHTML = isVisible ? '📋 Legend ▼' : '📋 Legend ▶';
            };
            
            // Initialize button text
            toggleBtn.innerHTML = '📋 Legend ▼';
            
            L.DomEvent.disableClickPropagation(container);
            L.DomEvent.disableScrollPropagation(container);
            return container;
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


def _rating_color(rating: float | None) -> str:
    """Return a color for school rating on a red-to-blue gradient.
    
    Color scale (intuitive bad-to-good):
    - Purple (#7B1FA2): No rating - stands out from the rating scale
    - Red (#E53935): Very poor (< 2.0)
    - Deep Orange (#F4511E): Poor (2.0 - 2.5)
    - Orange (#FB8C00): Below average (2.5 - 3.0)
    - Amber (#FFB300): Average (3.0 - 3.5)
    - Yellow-Green (#C0CA33): Above average (3.5 - 4.0)
    - Green (#43A047): Good (4.0 - 4.5)
    - Blue (#1E88E5): Excellent (4.5+)
    """
    if rating is None:
        return "#7B1FA2"  # Purple - no rating (stands out from red-to-blue scale)
    if rating < 2.0:
        return "#E53935"  # Red - very poor
    if rating < 2.5:
        return "#F4511E"  # Deep Orange - poor
    if rating < 3.0:
        return "#FB8C00"  # Orange - below average
    if rating < 3.5:
        return "#FFB300"  # Amber - average
    if rating < 4.0:
        return "#C0CA33"  # Yellow-Green - above average
    if rating < 4.5:
        return "#43A047"  # Green - good
    return "#1E88E5"      # Blue - excellent


def _is_secondary(level: str | None) -> bool:
    return (level or "").strip().lower() == "secondary"


def _urban_grade(value: int | str | None) -> str | None:
    if value is None:
        return None
    try:
        value_int = int(value)
    except (TypeError, ValueError):
        return str(value)
    if value_int == -2:
        return None
    if value_int == -1:
        return "PK"
    if value_int == 0:
        return "K"
    return str(value_int)


def render_schools() -> None:
    st.session_state.setdefault("schools_expanded", False)

    with st.expander("🎓 Schools & Districts", expanded=st.session_state["schools_expanded"]):
        st.subheader("Nearby Schools & District Assignment")

        locations = st.session_state.get("map_data", {}).get("locations", [])
        house = _get_loc_by_label(locations, "House")

        if not house:
            st.warning("Add a location labeled **House** to enable school mapping.")
            return

        radius_miles = st.slider(
            "Search radius (miles)",
            min_value=1,
            max_value=50,
            step=1,
            key="schools_radius_miles",
        )

        api_key = load_google_maps_api_key()
        if not api_key:
            st.error("Google Maps API key missing. Schools layer cannot load.")
            return

        app_id, app_key = load_schooldigger_keys()
        if not app_id or not app_key:
            st.warning("SchoolDigger API keys missing. Academic enrichment will be skipped.")

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

        project = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True).transform
        house_point_m = transform(project, Point(house["lon"], house["lat"]))
        radius_meters = radius_miles * 1609.34
        search_area = house_point_m.buffer(radius_meters)

        search_area_latlon = transform(
            pyproj.Transformer.from_crs("EPSG:3857", "EPSG:4326", always_xy=True).transform,
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
            control=False,
        ).add_to(m)

        places_cache_key = (
            round(house["lat"], 4),
            round(house["lon"], 4),
            round(radius_miles, 2),
        )

        # Check if we need to load new data
        needs_places_load = (
            "schools_places" not in st.session_state
            or st.session_state.get("schools_places_key") != places_cache_key
        )
        needs_digger_load = (
            app_id and app_key and (
                "schools_digger" not in st.session_state
                or st.session_state.get("schools_digger_key") != places_cache_key
            )
        )

        # Show status bar when loading data
        if needs_places_load or needs_digger_load:
            status_container = st.status("🔄 Loading school data...", expanded=True)
            status_container.write(f"📍 **Location:** {house.get('address', 'Unknown')}")
            status_container.write(f"🔍 **Search radius:** {radius_miles} miles ({int(radius_meters)} meters)")
        else:
            status_container = None

        # Step 1: Fetch Google Places schools
        if needs_places_load:
            if status_container:
                status_container.write("---")
                status_container.write("📍 **Step 1/5:** Fetching nearby schools from Google Places...")
            places = fetch_google_places_schools(
                api_key=api_key,
                lat=house["lat"],
                lon=house["lon"],
                radius_meters=int(radius_meters),
            )
            st.session_state["schools_places"] = [
                parse_google_school(place, house_lat=house["lat"], house_lon=house["lon"])
                for place in places
            ]
            st.session_state["schools_places_key"] = places_cache_key
            if status_container:
                # Count school types
                primary_count = sum(1 for p in st.session_state["schools_places"] if p.get("level") == "Primary")
                secondary_count = sum(1 for p in st.session_state["schools_places"] if p.get("level") == "Secondary")
                status_container.write(f"   ✓ Found **{len(places)} schools** from Google Places")
                status_container.write(f"     • Primary/Elementary: {primary_count}")
                status_container.write(f"     • Secondary/High: {secondary_count}")
                status_container.write(f"     • Other: {len(places) - primary_count - secondary_count}")

        place_rows = st.session_state.get("schools_places", [])

        # Step 2: Determine state and fetch SchoolDigger data
        digger_rows = []
        state_abbrev = None
        if app_id and app_key:
            state_abbrev = (house.get("state") or house.get("state_abbrev") or "").strip().upper()
            if not state_abbrev:
                if status_container:
                    status_container.write("---")
                    status_container.write("🗺️ **Step 2/5:** Determining state from coordinates...")
                _, state_abbrev = reverse_geocode_county_state(
                    house["lat"],
                    house["lon"],
                    address_fallback=house.get("address"),
                )
                if status_container and state_abbrev:
                    status_container.write(f"   ✓ Detected state: **{state_abbrev}**")
            if state_abbrev:
                if needs_digger_load:
                    if status_container:
                        status_container.write("---")
                        status_container.write(f"🏫 **Step 2/5:** Fetching academic data from SchoolDigger...")
                        status_container.write(f"   • State: {state_abbrev}")
                        status_container.write(f"   • Bounding box: {radius_miles} mi radius")
                    digger = fetch_schooldigger_schools(
                        app_id=app_id,
                        app_key=app_key,
                        lat=house["lat"],
                        lon=house["lon"],
                        radius_miles=radius_miles,
                        state=state_abbrev,
                    )
                    st.session_state["schools_digger"] = [
                        parse_schooldigger_school(item) for item in digger
                    ]
                    st.session_state["schools_digger_key"] = places_cache_key
                    if status_container:
                        parsed_digger = st.session_state["schools_digger"]
                        elem_count = sum(1 for s in parsed_digger if s.get("level") == "Elementary")
                        middle_count = sum(1 for s in parsed_digger if s.get("level") == "Middle")
                        high_count = sum(1 for s in parsed_digger if s.get("level") == "High")
                        private_count = sum(1 for s in parsed_digger if s.get("is_private"))
                        status_container.write(f"   ✓ Found **{len(digger)} schools** from SchoolDigger")
                        status_container.write(f"     • Elementary: {elem_count}")
                        status_container.write(f"     • Middle: {middle_count}")
                        status_container.write(f"     • High: {high_count}")
                        status_container.write(f"     • Private: {private_count}")
                digger_rows = st.session_state.get("schools_digger", [])
            else:
                st.info("Unable to infer state for SchoolDigger enrichment.")

        # Step 3: Match schools between Google Places and SchoolDigger
        if status_container:
            status_container.write("---")
            status_container.write("🔗 **Step 3/5:** Matching schools between data sources...")
            status_container.write(f"   • Google Places schools: {len(place_rows)}")
            status_container.write(f"   • SchoolDigger schools: {len(digger_rows)}")
        matches, match_scores = match_schooldigger_to_places(
            place_rows,
            digger_rows,
            distance_threshold_miles=radius_miles,
            score_threshold=0.65,
        )
        if status_container:
            match_rate = (len(matches) / len(place_rows) * 100) if place_rows else 0
            avg_score = sum(match_scores.values()) / len(match_scores) if match_scores else 0
            status_container.write(f"   ✓ Matched **{len(matches)} of {len(place_rows)}** schools ({match_rate:.0f}%)")
            if match_scores:
                status_container.write(f"     • Average match score: {avg_score:.2f}")

        # Step 4: Fetch Urban Institute data for additional enrichment
        urban_rows = []
        urban_error = None
        if state_abbrev:
            needs_urban_load = (
                "schools_urban" not in st.session_state
                or st.session_state.get("schools_urban_key") != state_abbrev
            )
            if needs_urban_load:
                if status_container:
                    status_container.write("---")
                    status_container.write(f"📊 **Step 4/5:** Fetching federal school data from Urban Institute...")
                    status_container.write(f"   • State: {state_abbrev}")
                    status_container.write(f"   • Year: 2022 (latest available)")
                try:
                    st.session_state["schools_urban"] = fetch_urban_institute_schools_by_state(
                        state=state_abbrev
                    )
                    st.session_state["schools_urban_key"] = state_abbrev
                    if status_container:
                        status_container.write(
                            f"   ✓ Loaded **{len(st.session_state['schools_urban'])} schools** from Urban Institute"
                        )
                except requests.exceptions.RequestException as exc:
                    urban_error = (
                        "Urban Institute data is temporarily unavailable due to a network timeout. "
                        "We'll continue loading other school data sources."
                    )
                    st.session_state["schools_urban"] = []
                    st.session_state["schools_urban_key"] = state_abbrev
                    if status_container:
                        status_container.write(f"   ⚠️ {urban_error}")
                    st.warning(f"Urban Institute lookup failed: {exc}")
            urban_rows = st.session_state.get("schools_urban", [])

        # Update status to complete
        if status_container:
            status_container.write("---")
            if urban_error:
                status_container.write("⚠️ **Loaded with warnings:** Urban Institute data could not be reached.")
                status_container.update(
                    label=f"⚠️ School data loaded with warnings ({len(place_rows)} schools, {len(matches)} matched)",
                    state="error",
                    expanded=False,
                )
            else:
                status_container.write("✅ **Complete:** All school data loaded successfully!")
                status_container.update(
                    label=f"✅ School data loaded ({len(place_rows)} schools, {len(matches)} matched)",
                    state="complete",
                    expanded=False,
                )

        schools_group = folium.FeatureGroup(
            name=f"Schools ({len(place_rows)})",
            show=True,
        )

        # First pass: filter places to only those within search radius
        places_in_radius = []
        for place in place_rows:
            lat = place.get("lat")
            lon = place.get("lon")
            if lat is None or lon is None:
                continue
            point_m = transform(project, Point(lon, lat))
            if point_m.intersects(search_area):
                places_in_radius.append(place)
        
        # Filter matches to only include places within radius
        filtered_matches = {pid: school for pid, school in matches.items() 
                          if any(p.get("place_id") == pid for p in places_in_radius)}
        filtered_match_scores = {pid: score for pid, score in match_scores.items() 
                                if pid in filtered_matches}

        table_rows = []
        for place in places_in_radius:
            lat = place.get("lat")
            lon = place.get("lon")

            rating = place.get("rating")
            color = _rating_color(rating)
            level = place.get("level")
            tooltip_lines = [
                f"<b>{place.get('name') or 'School'}</b>",
                f"Level: {level}",
            ]
            if rating is not None:
                tooltip_lines.append(f"Rating: {rating} ({place.get('review_count') or 0} reviews)")
            if place.get("phone"):
                tooltip_lines.append(f"Phone: {place.get('phone')}")
            if place.get("website"):
                tooltip_lines.append(f"Website: {place.get('website')}")

            if _is_secondary(level):
                folium.RegularPolygonMarker(
                    location=[lat, lon],
                    number_of_sides=3,
                    radius=7,
                    color=color,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.85,
                    weight=1,
                    tooltip=folium.Tooltip("<br>".join(tooltip_lines)),
                ).add_to(schools_group)
            else:
                folium.CircleMarker(
                    location=[lat, lon],
                    radius=6,
                    color=color,
                    fill=True,
                    fill_color=color,
                    fill_opacity=0.85,
                    weight=1,
                    tooltip=folium.Tooltip("<br>".join(tooltip_lines)),
                ).add_to(schools_group)

            matched = matches.get(place.get("place_id"))
            if matched:
                place["district_name"] = matched.get("district_name")
                place["low_grade"] = matched.get("low_grade")
                place["high_grade"] = matched.get("high_grade")
                place["rank_history"] = matched.get("rank_history")
            elif urban_rows:
                city = place.get("city")
                state = place.get("state")
                if city and state:
                    candidates = [
                        row
                        for row in urban_rows
                        if normalize_name(row.get("city_location")) == normalize_name(city)
                        and normalize_name(row.get("state_location")) == normalize_name(state)
                    ]
                    best_row = None
                    best_score = 0.0
                    for row in candidates:
                        score = fuzzy_match_score(place.get("name"), row.get("school_name"))
                        if score > best_score:
                            best_row = row
                            best_score = score
                    if best_row and best_score >= 0.6:
                        place["district_name"] = best_row.get("lea_name")
                        place["low_grade"] = _urban_grade(best_row.get("lowest_grade_offered"))
                        place["high_grade"] = _urban_grade(best_row.get("highest_grade_offered"))

            grade_band = grade_range(
                place.get("low_grade"),
                place.get("high_grade"),
            )
            rank_history = place.get("rank_history") or []
            latest_rank = None
            if rank_history:
                latest_rank = max(
                    rank_history,
                    key=lambda item: item.get("year", 0),
                )
            state_rank = latest_rank.get("rank") if latest_rank else None
            rank_stars = latest_rank.get("rankStars") if latest_rank else None
            state_rank_value = str(state_rank) if state_rank is not None else "—"
            rank_stars_value = str(rank_stars) if rank_stars is not None else "—"

            # Build GreatSchools URL with lat/lon for precise location-based results
            gs_search_url = build_greatschools_search_url(
                place.get("name"),
                place.get("city"),
                place.get("state"),
                lat=place.get("lat"),
                lon=place.get("lon"),
                distance=radius_miles,
            )
            
            # Build Niche URL
            niche_url = build_niche_url(
                place.get("name"),
                place.get("city"),
                place.get("state"),
            )

            table_rows.append({
                "Name": place.get("name") or "—",
                "Level": level or "—",
                "Rating": rating,
                "Reviews": place.get("review_count"),
                "Distance (mi)": round(place.get("distance_mi"), 2) if place.get("distance_mi") else None,
                "Phone": place.get("phone") or "—",
                "Website": place.get("website") or "—",
                "Grades": grade_band or "—",
                "Type": "Private" if matched and matched.get("is_private") else "Public",
                "District": place.get("district_name") or "—",
                "State Rank": state_rank_value,
                "Rank Stars": rank_stars_value,
                "GreatSchools": gs_search_url,
                "Niche": niche_url,
            })

        schools_group.add_to(m)

        # Step 5: Fetch district boundaries
        district_geojson = None
        state_fips = (house.get("state_fips") or "").strip()
        if not state_fips:
            if not state_abbrev:
                state_abbrev = (house.get("state") or house.get("state_abbrev") or "").strip().upper()
            if not state_abbrev:
                _, state_abbrev = reverse_geocode_county_state(
                    house["lat"],
                    house["lon"],
                    address_fallback=house.get("address"),
                )
            state_fips = STATE_FIPS.get(state_abbrev or "", "")
        if state_fips:
            needs_district_load = (
                "schools_district_geojson" not in st.session_state
                or st.session_state.get("schools_district_key") != state_fips
            )
            if needs_district_load:
                # Show status for district loading if not already showing
                district_status = st.status("🗺️ Loading district boundaries...", expanded=True)
                district_status.write(f"🗺️ **Step 5/5:** Fetching district boundaries from NCES...")
                district_status.write(f"   • State FIPS: {state_fips}")
                district_status.write(f"   • Source: NCES EDGE Open Data API")
                st.session_state["schools_district_geojson"] = fetch_nces_district_boundaries(
                    state_fips=state_fips
                )
                st.session_state["schools_district_key"] = state_fips
                geojson = st.session_state["schools_district_geojson"]
                feature_count = len(geojson.get("features", [])) if geojson else 0
                district_status.write(f"   ✓ Loaded **{feature_count} district polygons**")
                district_status.update(
                    label=f"✅ District boundaries loaded ({feature_count} districts)",
                    state="complete",
                    expanded=False
                )
            district_geojson = st.session_state.get("schools_district_geojson")
        else:
            st.info("Set a state for the House location to enable district boundaries.")

        district_features = []
        if district_geojson:
            district_features = clip_geojson_to_radius(
                district_geojson,
                center_lat=house["lat"],
                center_lon=house["lon"],
                radius_miles=radius_miles,
            )

        if district_features:
            folium.GeoJson(
                {"type": "FeatureCollection", "features": district_features},
                name="District boundaries",
                style_function=lambda _: {
                    "color": "#3949AB",
                    "weight": 2,
                    "fillOpacity": 0,
                },
                tooltip=folium.GeoJsonTooltip(
                    fields=["NAME", "GEOID"],
                    aliases=["District", "GEOID"],
                    sticky=True,
                ),
                control=True,
                show=False,
            ).add_to(m)

        add_leaflet_legend_control(
            m,
            html=_load_legend("schools_legend.html"),
            position="bottomleft",
            container_style=(
                "background: white; padding: 10px 14px; border-radius: 6px; "
                "box-shadow: 0 2px 6px rgba(0,0,0,0.3); font-size: 13px; "
                "max-width: 260px; pointer-events: none;"
            ),
        )
        folium.LayerControl(collapsed=False, position="topright").add_to(m)

        st_folium(m, width=900, height=500, returned_objects=[])

        # Split schools into district schools vs other educational facilities
        district_schools = [row for row in table_rows if row.get("District") and row.get("District") != "—"]
        other_facilities = [row for row in table_rows if not row.get("District") or row.get("District") == "—"]
        
        # Table 1: District Schools (traditional K-12 schools)
        st.markdown("### 🏫 District Schools")
        st.caption("Traditional K-12 schools with district assignments — the schools kids attend daily")
        if district_schools:
            df_district = pd.DataFrame(district_schools)
            # Reorder columns to prioritize important info (including review site links)
            # Rating/Reviews omitted - use GreatSchools/Niche links for reviews instead
            district_columns = [
                "Name", "Level", "Grades", "District",
                "State Rank", "Rank Stars", "Type", "Distance (mi)", 
                "GreatSchools", "Niche", "Phone", "Website"
            ]
            # Only include columns that exist
            district_columns = [c for c in district_columns if c in df_district.columns]
            df_district = df_district[district_columns]
            df_district = df_district.sort_values(["Distance (mi)"], ascending=[True])
            
            # Configure clickable link columns
            column_config = {
                "GreatSchools": st.column_config.LinkColumn(
                    "GreatSchools",
                    help="View this school on GreatSchools.org with ratings and reviews",
                    display_text="🏫 View",
                ),
                "Niche": st.column_config.LinkColumn(
                    "Niche",
                    help="View this school on Niche.com with grades and reviews",
                    display_text="📊 View",
                ),
                "Website": st.column_config.LinkColumn(
                    "Website",
                    help="School website",
                    display_text="🌐 Site",
                ),
            }
            
            st.dataframe(
                df_district, 
                width="stretch", 
                hide_index=True,
                height=min(400, 35 * len(district_schools) + 38),
                column_config=column_config,
            )
        else:
            st.caption("No district schools found within this radius.")
        
        # Table 2: Other Educational Facilities (swim schools, daycares, tutoring, etc.)
        st.markdown("### 📚 Other Educational Facilities")
        st.caption("Specialty programs, preschools, daycares, and enrichment centers (not traditional K-12)")
        if other_facilities:
            df_other = pd.DataFrame(other_facilities)
            # Simplified columns for non-district facilities (include review site links)
            other_columns = ["Name", "Level", "Rating", "Reviews", "Distance (mi)", "GreatSchools", "Niche", "Phone", "Website"]
            # Only include columns that exist
            other_columns = [c for c in other_columns if c in df_other.columns]
            df_other = df_other[other_columns]
            df_other = df_other.sort_values(["Distance (mi)", "Rating"], ascending=[True, False])
            
            # Configure clickable link columns for other facilities
            other_column_config = {
                "GreatSchools": st.column_config.LinkColumn(
                    "GreatSchools",
                    help="Search for this facility on GreatSchools.org",
                    display_text="🏫 View",
                ),
                "Niche": st.column_config.LinkColumn(
                    "Niche",
                    help="Search for this facility on Niche.com",
                    display_text="📊 View",
                ),
                "Website": st.column_config.LinkColumn(
                    "Website",
                    help="Facility website",
                    display_text="🌐 Site",
                ),
            }
            
            st.dataframe(
                df_other, 
                width="stretch", 
                hide_index=True,
                height=min(300, 35 * len(other_facilities) + 38),
                column_config=other_column_config,
            )
        else:
            st.caption("No other educational facilities found within this radius.")

        with st.expander("SchoolDigger match diagnostics", expanded=False):
            # Search parameters summary
            st.markdown("#### 🔍 Search Parameters")
            col1, col2 = st.columns(2)
            with col1:
                st.metric("House Location", f"{house.get('address', 'Unknown')[:40]}...")
                st.metric("Coordinates", f"{house['lat']:.4f}, {house['lon']:.4f}")
            with col2:
                st.metric("Search Radius", f"{radius_miles} miles")
                st.metric("State", state_abbrev or "Unknown")
            
            st.markdown("---")
            
            # Data source summary (use filtered data)
            st.markdown("#### 📊 Data Sources Summary")
            col1, col2, col3 = st.columns(3)
            with col1:
                st.metric("Google Places (in radius)", len(places_in_radius), help="Schools from Google Places API within search radius")
            with col2:
                st.metric("SchoolDigger", len(digger_rows), help="Schools from SchoolDigger API (bounding box)")
            with col3:
                match_rate = (len(filtered_matches) / len(places_in_radius) * 100) if places_in_radius else 0
                st.metric("Matched", f"{len(filtered_matches)} ({match_rate:.0f}%)", help="Successfully matched schools within radius")
            
            st.markdown("---")
            
            # Google Places breakdown (use filtered data)
            if places_in_radius:
                st.markdown("#### 📍 Google Places Schools (within radius)")
                primary_count = sum(1 for p in places_in_radius if p.get("level") == "Primary")
                secondary_count = sum(1 for p in places_in_radius if p.get("level") == "Secondary")
                other_count = len(places_in_radius) - primary_count - secondary_count
                
                places_summary = pd.DataFrame([
                    {"Level": "Primary/Elementary", "Count": primary_count},
                    {"Level": "Secondary/High", "Count": secondary_count},
                    {"Level": "Other", "Count": other_count},
                ])
                st.dataframe(places_summary, width="stretch", hide_index=True)
            
            # SchoolDigger breakdown
            if digger_rows:
                st.markdown("#### 🏫 SchoolDigger Schools (bounding box)")
                elem_count = sum(1 for s in digger_rows if s.get("level") == "Elementary")
                middle_count = sum(1 for s in digger_rows if s.get("level") == "Middle")
                high_count = sum(1 for s in digger_rows if s.get("level") == "High")
                private_count = sum(1 for s in digger_rows if s.get("is_private"))
                other_digger = len(digger_rows) - elem_count - middle_count - high_count
                
                digger_summary = pd.DataFrame([
                    {"Level": "Elementary", "Count": elem_count},
                    {"Level": "Middle", "Count": middle_count},
                    {"Level": "High", "Count": high_count},
                    {"Level": "Other/Unknown", "Count": other_digger},
                    {"Type": "Private", "Count": private_count},
                ])
                st.dataframe(digger_summary, width="stretch", hide_index=True)
            
            st.markdown("---")
            
            # Match results (use filtered matches - only schools within radius)
            if filtered_matches:
                st.markdown("#### ✅ Matched Schools (within radius)")
                avg_score = sum(filtered_match_scores.values()) / len(filtered_match_scores) if filtered_match_scores else 0
                st.caption(f"Average match score: **{avg_score:.3f}**")
                
                match_preview = []
                for place in places_in_radius:
                    pid = place.get("place_id")
                    if pid in filtered_matches:
                        matched_school = filtered_matches[pid]
                        match_preview.append({
                            "Google Places Name": place.get("name"),
                            "SchoolDigger Name": matched_school.get("name"),
                            "Match Score": f"{filtered_match_scores.get(pid, 0):.3f}",
                            "District": matched_school.get("district_name") or "—",
                            "Grades": f"{matched_school.get('low_grade', '—')}–{matched_school.get('high_grade', '—')}",
                            "Distance (mi)": f"{place.get('distance_mi', 0):.2f}" if place.get("distance_mi") else "—",
                        })
                
                if match_preview:
                    st.dataframe(pd.DataFrame(match_preview), width="stretch", hide_index=True)
            
            # Unmatched schools (use filtered data)
            unmatched_places = [p for p in places_in_radius if p.get("place_id") not in filtered_matches]
            if unmatched_places:
                st.markdown("#### ⚠️ Unmatched Google Places Schools (within radius)")
                st.caption(f"{len(unmatched_places)} schools could not be matched to SchoolDigger data")
                
                unmatched_data = []
                for place in unmatched_places:
                    unmatched_data.append({
                        "Name": place.get("name"),
                        "City": place.get("city") or "—",
                        "Level": place.get("level") or "—",
                        "Distance (mi)": f"{place.get('distance_mi', 0):.2f}" if place.get("distance_mi") else "—",
                    })
                
                if unmatched_data:
                    st.dataframe(
                        pd.DataFrame(unmatched_data),
                        width="stretch",
                        hide_index=True,
                        height=min(400, 35 * len(unmatched_data) + 38),  # Scrollable with max height
                    )

        if district_features:
            st.markdown("### District coverage")
            district_names = sorted({f.get("properties", {}).get("NAME") for f in district_features})
            if district_names:
                st.info(
                    "District boundaries loaded: " + ", ".join([n for n in district_names if n])
                )
