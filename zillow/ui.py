from __future__ import annotations

from typing import Any

import folium
from folium.plugins import Draw
import streamlit as st
from streamlit_folium import st_folium

from locations.logic import _get_loc_by_label
from locations.providers import geocode_once
from .providers import load_zillow_api_key, search_zillow_properties


# === Constants for Polygon Search ===

HOME_STATUS_OPTIONS = ["FOR_SALE", "FOR_RENT", "RECENTLY_SOLD"]

HOME_TYPE_OPTIONS = {
    "FOR_SALE": [
        "HOUSES",
        "TOWNHOMES",
        "MULTI_FAMILY",
        "CONDOS_COOPS",
        "LOTSLAND",
        "APARTMENTS",
        "MANUFACTURED",
    ],
    "RECENTLY_SOLD": [
        "HOUSES",
        "TOWNHOMES",
        "MULTI_FAMILY",
        "CONDOS_COOPS",
        "LOTSLAND",
        "APARTMENTS",
        "MANUFACTURED",
    ],
    "FOR_RENT": [
        "HOUSES",
        "APARTMENTS_CONDOS_COOPS",
        "TOWNHOMES",
    ],
}

SPACE_TYPE_OPTIONS = ["ENTIRE_PLACE", "ROOM"]

SORT_OPTIONS = {
    "FOR_SALE": [
        "DEFAULT",
        "PRICE_HIGH_LOW",
        "PRICE_LOW_HIGH",
        "NEWEST",
        "BEDROOMS",
        "BATHROOMS",
        "SQUARE_FEET",
        "LOT_SIZE",
    ],
    "RECENTLY_SOLD": [
        "DEFAULT",
        "PRICE_HIGH_LOW",
        "PRICE_LOW_HIGH",
        "NEWEST",
        "BEDROOMS",
        "BATHROOMS",
        "SQUARE_FEET",
        "LOT_SIZE",
    ],
    "FOR_RENT": [
        "DEFAULT",
        "VERIFIED_SOURCE",
        "PRICE_HIGH_LOW",
        "PRICE_LOW_HIGH",
        "NEWEST",
        "BEDROOMS",
        "BATHROOMS",
        "SQUARE_FEET",
        "LOT_SIZE",
    ],
}

LISTING_TYPE_OPTIONS = ["BY_AGENT", "BY_OWNER_OTHER"]


# === Status Constants ===

class SearchStatus:
    IDLE = "Idle / Waiting for input"
    VALIDATING = "Validating inputs..."
    BUILDING_QUERY = "Building search query..."
    CALLING_API = "Calling Zillow API..."
    PARSING = "Parsing results..."
    RENDERING = "Rendering results..."
    DONE = "Done"
    ERROR = "Error"
    NO_POLYGON = "No polygon drawn"


# === Session State Initialization ===

# === Pagination Constants ===
RESULTS_PER_PAGE = 10


def _init_zillow_state() -> None:
    """Initialize all session state variables for Zillow polygon search."""
    # Always default the Zillow expander to collapsed on load.
    st.session_state["zillow_expanded"] = False
    defaults = {
        "zillow_status": SearchStatus.IDLE,
        "zillow_progress": 0,
        "zillow_progress_label": "Idle",
        "zillow_results": [],
        "zillow_error": None,
        "zillow_polygon_coords": [],
        "zillow_last_query": None,
        "zillow_last_polygon": [],
        "zillow_search_triggered": False,
        "zillow_api_metadata": None,  # Store API response metadata
        "zillow_results_page": 1,  # Current page for results display
        "zillow_results_expanded": True,  # Results section expanded state
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _update_status(message: str, progress: int, label: str | None = None) -> None:
    """Update status panel state."""
    st.session_state["zillow_status"] = message
    st.session_state["zillow_progress"] = progress
    if label is not None:
        st.session_state["zillow_progress_label"] = label


def _clear_error() -> None:
    """Clear any existing error state."""
    st.session_state["zillow_error"] = None


# === Polygon Handling ===

def _extract_polygon_coords(draw_data: dict | None) -> list[list[float]]:
    """
    Extract polygon coordinates from Folium draw data.
    
    Returns list of [lon, lat] pairs suitable for API format.
    """
    if not draw_data:
        return []
    
    polygon = draw_data.get("last_active_drawing")
    if not polygon:
        # Fall back to the last geojson feature when edits are made
        geojson = draw_data.get("last_active_drawing") or draw_data.get("all_drawings")
        if isinstance(geojson, list) and geojson:
            polygon = geojson[-1]
        elif isinstance(geojson, dict):
            polygon = geojson
        else:
            return []
    
    geometry = polygon.get("geometry") or {}
    coords = geometry.get("coordinates")
    if not coords:
        return []
    
    # Leaflet returns list of rings; use the first ring
    ring = coords[0] if isinstance(coords, list) else []
    if not ring:
        return []
    
    # Convert to [lon, lat] pairs
    simplified = []
    for point in ring:
        if isinstance(point, (list, tuple)) and len(point) >= 2:
            lon, lat = point[0], point[1]
            simplified.append([float(lon), float(lat)])
    
    return simplified


def _format_polygon_for_api(coords: list[list[float]]) -> str | None:
    """
    Format polygon coordinates for the Zillow API.
    
    Format: "lon lat, lon lat, lon lat, ..."
    """
    if not coords:
        return None
    parts = [f"{lon} {lat}" for lon, lat in coords]
    return ", ".join(parts)


def _polygon_coords_to_latlng(coords: list[list[float]]) -> list[list[float]]:
    """Convert polygon coordinates [lon, lat] to [lat, lon] for Folium."""
    if not coords:
        return []
    return [[lat, lon] for lon, lat in coords]


def _format_polygon_display(coords: list[list[float]]) -> str:
    """Format polygon coordinates for UI display."""
    if not coords:
        return "No polygon drawn"
    return f"{len(coords)} points: {_format_polygon_for_api(coords)}"




# === Result Formatting ===

def _format_price(value: Any) -> str:
    """Format price value for display."""
    if value is None or value == "":
        return "—"
    try:
        return f"${float(value):,.0f}"
    except (TypeError, ValueError):
        return str(value)


def _format_result_row(result: dict) -> dict:
    """Format a single search result for display."""
    # Handle nested address object or flat structure
    address = result.get("address") if isinstance(result.get("address"), dict) else {}
    
    address_line = (
        address.get("streetAddress") 
        or result.get("streetAddress") 
        or result.get("address")
    )
    city = address.get("city") or result.get("city")
    state = address.get("state") or result.get("state")
    zipcode = address.get("zipcode") or result.get("zip") or result.get("zipcode")
    
    price = result.get("price") or result.get("unformattedPrice")
    beds = result.get("bedrooms") or result.get("beds")
    baths = result.get("bathrooms") or result.get("baths")
    sqft = result.get("livingArea") or result.get("area") or result.get("sqft")
    home_type = result.get("homeType") or result.get("home_type")
    status = result.get("homeStatus") or result.get("home_status")
    detail_url = result.get("detailUrl") or result.get("url") or result.get("hdpUrl")
    img_url = result.get("imgSrc") or result.get("image") or result.get("photo")
    whats_special = (
        result.get("whatsSpecial")
        or result.get("whats_special")
        or result.get("specialFeatures")
        or result.get("special_features")
        or result.get("highlights")
        or result.get("homeHighlights")
        or result.get("listingHighlights")
    )
    
    city_state_zip = " ".join([p for p in [city, state, zipcode] if p])
    address_label = " · ".join([part for part in [address_line, city_state_zip] if part])
    
    return {
        "address": address_label or "—",
        "price": _format_price(price),
        "beds": beds or "—",
        "baths": baths or "—",
        "sqft": sqft or "—",
        "home_type": home_type or "—",
        "status": status or "—",
        "detail_url": detail_url,
        "img_url": img_url,
        "whats_special": whats_special,
        "raw": result,
    }


def _render_whats_special(details: Any) -> None:
    """Render a Zillow-style What's special dropdown if details are provided."""
    if not details:
        return

    lines: list[str] = []
    if isinstance(details, str):
        lines = [details.strip()] if details.strip() else []
    elif isinstance(details, list):
        lines = [str(item).strip() for item in details if str(item).strip()]
    elif isinstance(details, dict):
        for key, value in details.items():
            if value is None or value == "":
                continue
            if isinstance(value, list):
                formatted = ", ".join([str(item).strip() for item in value if str(item).strip()])
                if formatted:
                    lines.append(f"{key}: {formatted}")
            else:
                lines.append(f"{key}: {value}")
    else:
        lines = [str(details).strip()] if str(details).strip() else []

    if not lines:
        return

    with st.expander("✨ What's special", expanded=False):
        for line in lines:
            st.write(f"• {line}")


# === Validation ===

def _normalize_home_types(home_status: str, selected: list[str]) -> list[str]:
    """Filter home types to only those valid for the current home status."""
    allowed = set(HOME_TYPE_OPTIONS.get(home_status, []))
    return [item for item in selected if item in allowed]


def _validate_polygon_search(polygon_coords: list[list[float]], params: dict[str, Any]) -> list[str]:
    """
    Validate inputs for polygon search.
    
    For polygon search, polygon is REQUIRED and location must be OMITTED.
    """
    errors = []
    
    # Polygon is required for this mode
    if not polygon_coords or len(polygon_coords) < 3:
        errors.append("Polygon with at least 3 points is required. Draw a polygon on the map.")
    
    # Validate page range
    page = params.get("page", 1)
    if page < 1 or page > 100:
        errors.append("Page must be between 1 and 100.")
    
    # Validate min/max pairs
    pairs = [
        ("min_price", "max_price", "price"),
        ("min_monthly_payment", "max_monthly_payment", "monthly payment"),
        ("min_bedrooms", "max_bedrooms", "bedrooms"),
        ("min_bathrooms", "max_bathrooms", "bathrooms"),
        ("min_sqft", "max_sqft", "square feet"),
        ("min_lot_size", "max_lot_size", "lot size"),
    ]
    for low_key, high_key, label in pairs:
        low = params.get(low_key)
        high = params.get(high_key)
        if low is not None and high is not None and low != "" and high != "":
            try:
                if float(low) > float(high):
                    errors.append(f"Minimum {label} must be ≤ maximum {label}.")
            except (TypeError, ValueError):
                continue
    
    return errors


# === Query Building ===

def _build_polygon_query(params: dict[str, Any], polygon_coords: list[list[float]]) -> dict[str, Any]:
    """
    Build query parameters for polygon search endpoint.
    
    Note: For polygon search, location MUST be omitted.
    """
    # Start with a clean dict, excluding None/empty values
    query = {}
    
    for k, v in params.items():
        # Skip location - not allowed for polygon search
        if k == "location":
            continue
        # Skip empty values
        if v in (None, "", [], False):
            continue
        # Include the parameter
        query[k] = v
    
    # Add formatted polygon (REQUIRED for polygon search)
    polygon_value = _format_polygon_for_api(polygon_coords)
    if polygon_value:
        query["polygon"] = polygon_value
    
    # Convert lists to comma-separated strings
    if isinstance(query.get("home_type"), list):
        query["home_type"] = ",".join(query["home_type"])
    if isinstance(query.get("space_type"), list):
        query["space_type"] = ",".join(query["space_type"])
    
    return query


# === Location Management Integration ===

def _add_house_from_result(result: dict) -> None:
    """Add a property from search results to Location Management."""
    # Extract address components
    address = result.get("address") if isinstance(result.get("address"), dict) else {}
    
    address_line = (
        address.get("streetAddress") 
        or result.get("streetAddress") 
        or result.get("address")
    )
    city = address.get("city") or result.get("city")
    state = address.get("state") or result.get("state")
    zipcode = address.get("zipcode") or result.get("zip") or result.get("zipcode")
    
    parts = [part for part in [address_line, city, state, zipcode] if part]
    if not parts:
        st.warning("Zillow record is missing an address.")
        return
    
    # Format full address
    if len(parts) >= 3:
        full_address = ", ".join(parts[:-2] + [" ".join(parts[-2:])])
    else:
        full_address = ", ".join(parts)
    
    # Get coordinates
    try:
        lat = result.get("latitude") or result.get("lat")
        lon = result.get("longitude") or result.get("lon")
        
        # Check nested latLong object
        if lat is None or lon is None:
            lat_long = result.get("latLong", {})
            lat = lat_long.get("latitude") or lat
            lon = lat_long.get("longitude") or lon
        
        # Geocode if still missing
        if lat is None or lon is None:
            lat, lon = geocode_once(full_address)
    except Exception as exc:
        st.error(f"Unable to geocode address: {exc}")
        return
    
    # Update Location Management
    locations = st.session_state.get("map_data", {}).get("locations", [])
    updated = False
    
    for loc in locations:
        if loc.get("label", "").strip().lower() == "house":
            loc.update({
                "label": "House",
                "address": full_address,
                "lat": lat,
                "lon": lon,
            })
            updated = True
            break
    
    if not updated:
        locations.append({
            "label": "House",
            "address": full_address,
            "lat": lat,
            "lon": lon,
        })
    
    st.session_state["map_data"] = {"locations": locations}
    st.session_state["map_badge"] = f"{len(locations)} locations"
    st.session_state["map_expanded"] = True
    st.success("✅ Added House to Location Management.")
    st.rerun()


# === Progressive Search Execution with Placeholders ===

def _execute_polygon_search_with_progress(
    params: dict[str, Any], 
    polygon_coords: list[list[float]],
    status_container,
) -> None:
    """
    Execute the polygon search API call with progressive status updates.
    Uses Streamlit placeholders for real-time feedback.
    """
    _clear_error()
    st.session_state["zillow_api_metadata"] = None
    
    def update_progress(message: str, progress: int, label: str):
        """Update the UI placeholders with current progress."""
        _update_status(message, progress, label)
    
    try:
        # Step 1: Validate
        update_progress(SearchStatus.VALIDATING, 10, "Step 1/5: Validating inputs...")
        validation_errors = _validate_polygon_search(polygon_coords, params)
        if validation_errors:
            st.session_state["zillow_error"] = "\n".join(validation_errors)
            update_progress(SearchStatus.ERROR, 0, "Validation Failed")
            return
        
        # Step 2: Build query
        update_progress(SearchStatus.BUILDING_QUERY, 25, "Step 2/5: Building search query...")
        query = _build_polygon_query(params, polygon_coords)
        
        # Step 3: Call API
        update_progress(SearchStatus.CALLING_API, 50, "Step 3/5: Calling Zillow API...")
        api_key = load_zillow_api_key()
        response = search_zillow_properties(
            api_key=api_key,
            params=query,
            use_polygon=True,
        )
        
        # Extract and store API metadata
        api_metadata = {
            "status": response.get("status"),
            "request_id": response.get("request_id"),
            "parameters": response.get("parameters", {}),
            "total_results": len(response.get("data") or response.get("results") or []),
        }
        st.session_state["zillow_api_metadata"] = api_metadata
        
        # Step 4: Parse results
        update_progress(SearchStatus.PARSING, 75, "Step 4/5: Parsing results...")
        results = response.get("results") or response.get("data") or []
        formatted_results = [_format_result_row(item) for item in results]
        
        # Step 5: Store and render
        update_progress(SearchStatus.RENDERING, 90, "Step 5/5: Rendering results...")
        st.session_state["zillow_results"] = formatted_results
        st.session_state["zillow_last_query"] = query
        st.session_state["zillow_last_polygon"] = polygon_coords
        st.session_state["zillow_results_page"] = 1
        st.session_state["zillow_search_triggered"] = False
        
        # Done
        result_count = len(formatted_results)
        update_progress(SearchStatus.DONE, 100, f"Complete: Found {result_count} properties")
        
    except Exception as exc:
        st.session_state["zillow_results"] = []
        st.session_state["zillow_error"] = str(exc)
        update_progress(SearchStatus.ERROR, 0, "Search Failed")


# === Main UI Render ===

def render_zillow_search() -> None:
    """Render the Zillow Polygon Search UI section."""
    _init_zillow_state()
    
    with st.expander("🏠 Zillow Property Search (Polygon)", expanded=st.session_state["zillow_expanded"]):
        st.subheader("Search by Polygon")
        st.caption("Draw a polygon on the map to search for properties within that area.")
        
        # Show current house location if available
        locations = st.session_state.get("map_data", {}).get("locations", [])
        house = _get_loc_by_label(locations, "House")
        if house:
            st.info(f"📍 Current House: {house.get('address', 'Unknown address')}")
        
        # === Map Section ===
        st.markdown("### Draw Search Area")
        
        # Determine map center
        if house:
            map_center = [house["lat"], house["lon"]]
            zoom_start = 12
        else:
            map_center = [39.5, -98.35]  # Center of US
            zoom_start = 5
        
        # Create map with drawing tools
        m = folium.Map(location=map_center, zoom_start=zoom_start, tiles="OpenStreetMap")
        feature_group = folium.FeatureGroup(name="Drawn Shapes")
        feature_group.add_to(m)
        
        # Re-add saved polygon to the map so it persists across rerenders
        saved_polygon = st.session_state.get("zillow_polygon_coords", [])
        saved_latlng = _polygon_coords_to_latlng(saved_polygon)
        if len(saved_latlng) >= 3:
            folium.Polygon(
                locations=saved_latlng,
                color="#1f77b4",
                weight=3,
                fill=True,
                fill_opacity=0.2,
            ).add_to(feature_group)
        Draw(
            export=False,
            show_geometry_on_click=False,
            feature_group=feature_group,
            draw_options={
                "polygon": True,
                "polyline": False,
                "rectangle": False,
                "circle": False,
                "marker": False,
                "circlemarker": False,
            },
            edit_options={
                "edit": {
                    "selectedPathOptions": {
                        "maintainColor": True,
                        "opacity": 0.7,
                        "fillOpacity": 0.2,
                    }
                },
                "remove": True,
            },
        ).add_to(m)

        # Enable vertex removal with Shift/Alt click in edit mode
        vertex_delete_script = folium.Element(
            """
<script>
(function() {
  if (!window.L || !L.Edit || !L.Edit.PolyVerticesEdit) return;
  var proto = L.Edit.PolyVerticesEdit.prototype;
  if (proto.__vertexDeletePatched) return;
  var original = proto._onMarkerClick;
  proto._onMarkerClick = function(e) {
    var evt = e.originalEvent || {};
    if (evt.shiftKey || evt.altKey) {
      this._removeMarker(e.target);
      this._fireEdit();
      return;
    }
    return original.call(this, e);
  };
  proto.__vertexDeletePatched = true;
})();
</script>
"""
        )
        m.get_root().html.add_child(vertex_delete_script)
        
        # Render map and capture drawing
        draw_data = st_folium(
            m,
            width=900,
            height=500,
            key="zillow_polygon_map",
            feature_group_to_add=feature_group,
        )
        
        # Extract polygon coordinates
        new_polygon_coords = _extract_polygon_coords(draw_data)
        if new_polygon_coords:
            st.session_state["zillow_polygon_coords"] = new_polygon_coords
        elif draw_data is not None and draw_data.get("all_drawings") == []:
            # Sync with delete tool (cleared in map UI)
            st.session_state["zillow_polygon_coords"] = []
            st.session_state["zillow_results"] = []
            st.session_state["zillow_api_metadata"] = None
            _update_status(SearchStatus.IDLE, 0, "Polygon cleared")
        
        polygon_coords = st.session_state.get("zillow_polygon_coords", [])
        
        # Polygon status indicator
        if polygon_coords and len(polygon_coords) >= 3:
            st.success(f"✅ Polygon drawn with {len(polygon_coords)} points")
        else:
            st.warning("⚠️ Draw a polygon on the map to enable search")

        st.caption("Tip: Use the pencil edit tool, then Shift+click or Alt+click a vertex to delete it.")
        
        # Polygon coordinates debug
        with st.expander("📐 Polygon Coordinates", expanded=False):
            if polygon_coords:
                st.code(_format_polygon_for_api(polygon_coords), language="text")
                st.json(polygon_coords)
            else:
                st.caption("No polygon drawn yet.")

        
        st.divider()
        
        # === Filter Form ===
        st.markdown("### Search Filters")
        
        with st.form("zillow_polygon_search_form"):
            # Listing Status Section
            col1, col2 = st.columns(2)
            
            with col1:
                home_status = st.selectbox(
                    "Home Status",
                    HOME_STATUS_OPTIONS,
                    index=0,
                    key="zillow_home_status",
                )
                listing_type = st.selectbox(
                    "Listing Type",
                    LISTING_TYPE_OPTIONS,
                    index=0,
                    key="zillow_listing_type",
                )
            
            with col2:
                home_type = st.multiselect(
                    "Home Type",
                    HOME_TYPE_OPTIONS.get(home_status, []),
                    key="zillow_home_type",
                )
                space_type = st.multiselect(
                    "Space Type (Rent only)",
                    SPACE_TYPE_OPTIONS,
                    disabled=home_status != "FOR_RENT",
                    key="zillow_space_type",
                )
            
            st.markdown("#### Price")
            price_cols = st.columns(2)
            with price_cols[0]:
                min_price = st.number_input("Min Price", min_value=0, value=350000, step=10000)
                min_price = None if min_price == 0 else min_price
            with price_cols[1]:
                max_price = st.number_input("Max Price", min_value=0, value=750000, step=10000)
                max_price = None if max_price == 0 else max_price
            
            min_monthly = None
            max_monthly = None
            
            st.markdown("#### Beds & Baths")
            bed_cols = st.columns(4)
            with bed_cols[0]:
                min_beds = st.number_input("Min Beds", min_value=0, value=3, step=1)
                min_beds = None if min_beds == 0 else min_beds
            with bed_cols[1]:
                max_beds = st.number_input("Max Beds", min_value=0, value=5, step=1)
                max_beds = None if max_beds == 0 else max_beds
            with bed_cols[2]:
                min_baths = st.number_input("Min Baths", min_value=0.0, value=2.0, step=0.5)
                min_baths = None if min_baths == 0 else min_baths
            with bed_cols[3]:
                max_baths = st.number_input("Max Baths", min_value=0.0, value=4.0, step=0.5)
                max_baths = None if max_baths == 0 else max_baths
            
            st.markdown("#### Size")
            size_cols = st.columns(4)
            with size_cols[0]:
                min_sqft = st.number_input("Min Sqft", min_value=0, value=1500, step=100)
                min_sqft = None if min_sqft == 0 else min_sqft
            with size_cols[1]:
                max_sqft = st.number_input("Max Sqft", min_value=0, value=3500, step=100)
                max_sqft = None if max_sqft == 0 else max_sqft
            with size_cols[2]:
                min_lot = st.number_input("Min Lot (sqft)", min_value=0, value=3000, step=100)
                min_lot = None if min_lot == 0 else min_lot
            with size_cols[3]:
                max_lot = st.number_input("Max Lot (sqft)", min_value=0, value=12000, step=100)
                max_lot = None if max_lot == 0 else max_lot
            
            # Additional filters (collapsible style using columns)
            with st.expander("Additional Filters", expanded=False):
                filter_cols = st.columns(2)
                with filter_cols[0]:
                    # Only show these for BY_OWNER_OTHER listing type
                    if listing_type == "BY_OWNER_OTHER":
                        for_sale_by_agent = st.checkbox("For Sale by Agent", value=True)
                        for_sale_by_owner = st.checkbox("For Sale by Owner", value=True)
                    else:
                        for_sale_by_agent = None
                        for_sale_by_owner = None
                    
                    for_sale_new = st.checkbox("New Construction", value=True)
                    for_sale_foreclosure = st.checkbox("Foreclosure (REO)", value=True)
                    for_sale_auction = st.checkbox("Auction", value=True)
                
                with filter_cols[1]:
                    for_sale_foreclosed = st.checkbox("Previously Foreclosed", value=False)
                    for_sale_preforeclosure = st.checkbox("Pre-Foreclosure", value=False)
                    max_hoa = st.number_input("Max HOA Fee", min_value=0, value=250, step=50)
                    max_hoa = None if max_hoa == 0 else max_hoa
                    include_no_hoa = st.checkbox("Include Homes Without HOA Data", value=True)
            
            st.markdown("#### Sorting & Paging")
            sort_cols = st.columns(2)
            with sort_cols[0]:
                sort_option = st.selectbox(
                    "Sort By",
                    SORT_OPTIONS.get(home_status, ["DEFAULT"]),
                    index=0,
                )
            with sort_cols[1]:
                page = st.number_input("Page", min_value=1, max_value=100, value=1, step=1)
            
            # Build params dict
            params = {
                "home_status": home_status,
                "listing_type": listing_type,
                "home_type": _normalize_home_types(home_status, home_type),
                "space_type": space_type if home_status == "FOR_RENT" else [],
                "min_price": min_price,
                "max_price": max_price,
                "min_bedrooms": min_beds,
                "max_bedrooms": max_beds,
                "min_bathrooms": min_baths,
                "max_bathrooms": max_baths,
                "min_sqft": min_sqft,
                "max_sqft": max_sqft,
                "min_lot_size": min_lot,
                "max_lot_size": max_lot,
                "for_sale_by_agent": for_sale_by_agent,
                "for_sale_by_owner": for_sale_by_owner,
                "for_sale_is_new_construction": for_sale_new,
                "for_sale_is_foreclosure": for_sale_foreclosure,
                "for_sale_is_auction": for_sale_auction,
                "for_sale_is_foreclosed": for_sale_foreclosed,
                "for_sale_is_preforeclosure": for_sale_preforeclosure,
                "max_hoa_fee": max_hoa,
                "includes_homes_no_hoa_data": include_no_hoa,
                "sort": sort_option,
                "page": page,
            }
            
            # Pre-validate for button state
            can_search = len(polygon_coords) >= 3
            validation_errors = _validate_polygon_search(polygon_coords, params)
            
            # Show validation warnings
            if validation_errors:
                for err in validation_errors:
                    st.warning(err)
            
            # Submit buttons
            submit_cols = st.columns([1, 1, 2])
            with submit_cols[0]:
                search_clicked = st.form_submit_button(
                    "🔍 Search Zillow",
                    type="primary",
                    disabled=not can_search,
                )
            with submit_cols[1]:
                clear_clicked = st.form_submit_button("🗑️ Clear Filters", type="secondary")
        
        # === Status Panel (BELOW the Search Button) ===
        # Only show status panel when there's meaningful status to display
        status_container = st.container()
        
        with status_container:
            status = st.session_state.get("zillow_status", SearchStatus.IDLE)
            progress = st.session_state.get("zillow_progress", 0)
            label = st.session_state.get("zillow_progress_label", "Idle")
            error = st.session_state.get("zillow_error")
            api_metadata = st.session_state.get("zillow_api_metadata")
            
            # Only show status section if there's something meaningful to show
            has_status_to_show = (
                status == SearchStatus.DONE
                or status == SearchStatus.ERROR
                or error
                or api_metadata
                or (progress > 0 and progress < 100)
            )
            
            if has_status_to_show:
                st.markdown("### Search Status")
                
                # Show current status
                if status == SearchStatus.DONE:
                    st.success(f"✅ {label}")
                elif status == SearchStatus.ERROR:
                    st.error(f"❌ {label}")
                elif progress > 0 and progress < 100:
                    st.info(f"🔄 {status}")
                    # Progress bar only during active search
                    st.progress(progress / 100, text=label)
                
                # Error details
                if error:
                    with st.expander("❌ Error Details", expanded=True):
                        st.error(error)
                
                # API Metadata (show if available)
                if api_metadata:
                    with st.expander("📊 API Response Metadata", expanded=False):
                        cols = st.columns(3)
                        with cols[0]:
                            st.metric("Status", api_metadata.get("status", "—"))
                        with cols[1]:
                            st.metric("Results", api_metadata.get("total_results", 0))
                        with cols[2]:
                            req_id = api_metadata.get("request_id", "—")
                            st.metric("Request ID", req_id[:12] + "..." if len(str(req_id)) > 12 else req_id)
                        
                        st.caption("**Query Parameters Used:**")
                        st.json(api_metadata.get("parameters", {}))
        
        # Handle form submissions
        if clear_clicked:
            st.session_state["zillow_results"] = []
            st.session_state["zillow_error"] = None
            st.session_state["zillow_api_metadata"] = None
            _update_status(SearchStatus.IDLE, 0, "Filters cleared")
            st.rerun()
        
        if search_clicked and can_search:
            st.session_state["zillow_search_triggered"] = True
            st.rerun()

        if st.session_state.get("zillow_search_triggered") and can_search:
            # Execute with progressive updates after session state sync
            _execute_polygon_search_with_progress(params, polygon_coords, status_container)
        
        # === Results Section (Paginated Collapsible) ===
        results = st.session_state.get("zillow_results", [])
        total_results = len(results)
        
        if results:
            # Calculate pagination
            current_page = st.session_state.get("zillow_results_page", 1)
            total_pages = max(1, (total_results + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
            
            # Ensure current page is valid
            if current_page > total_pages:
                current_page = total_pages
                st.session_state["zillow_results_page"] = current_page
            
            start_idx = (current_page - 1) * RESULTS_PER_PAGE
            end_idx = min(start_idx + RESULTS_PER_PAGE, total_results)
            page_results = results[start_idx:end_idx]
            
            # Collapsible results section
            results_expanded = st.session_state.get("zillow_results_expanded", True)
            with st.expander(
                f"📋 Search Results ({total_results} properties) — Page {current_page} of {total_pages}",
                expanded=results_expanded,
            ):
                # Pagination controls at top
                if total_pages > 1:
                    page_cols = st.columns([1, 2, 1, 2, 1])
                    with page_cols[0]:
                        if st.button("⏮️ First", key="zillow_page_first", disabled=current_page == 1):
                            st.session_state["zillow_results_page"] = 1
                            st.rerun()
                    with page_cols[1]:
                        if st.button("◀️ Previous", key="zillow_page_prev", disabled=current_page == 1):
                            st.session_state["zillow_results_page"] = current_page - 1
                            st.rerun()
                    with page_cols[2]:
                        st.markdown(f"**{current_page} / {total_pages}**")
                    with page_cols[3]:
                        if st.button("Next ▶️", key="zillow_page_next", disabled=current_page == total_pages):
                            st.session_state["zillow_results_page"] = current_page + 1
                            st.rerun()
                    with page_cols[4]:
                        if st.button("Last ⏭️", key="zillow_page_last", disabled=current_page == total_pages):
                            st.session_state["zillow_results_page"] = total_pages
                            st.rerun()
                    
                    st.caption(f"Showing {start_idx + 1}-{end_idx} of {total_results} properties")
                    st.divider()
                
                # Display results for current page
                for idx, item in enumerate(page_results):
                    global_idx = start_idx + idx
                    with st.container():
                        cols = st.columns([0.15, 0.55, 0.15, 0.15])
                        
                        with cols[0]:
                            if item.get("img_url"):
                                st.image(item["img_url"], width=120)
                            else:
                                st.caption("No image")
                        
                        with cols[1]:
                            st.markdown(f"**{item['address']}**")
                            st.caption(
                                f"{item['price']} · {item['beds']} bd · {item['baths']} ba · {item['sqft']} sqft"
                            )
                            st.caption(f"{item['home_type']} · {item['status']}")
                            if item.get("detail_url"):
                                st.markdown(f"[View on Zillow]({item['detail_url']})")
                        
                        with cols[2]:
                            if st.button("➕ Add House", key=f"zillow_add_{global_idx}"):
                                _add_house_from_result(item["raw"])
                        
                        with cols[3]:
                            st.caption("Source: Zillow")

                        _render_whats_special(item.get("whats_special"))
                    
                    if idx < len(page_results) - 1:
                        st.divider()
                
                # Pagination controls at bottom (for long pages)
                if total_pages > 1:
                    st.divider()
                    bottom_cols = st.columns([1, 2, 1, 2, 1])
                    with bottom_cols[0]:
                        if st.button("⏮️ First", key="zillow_page_first_btm", disabled=current_page == 1):
                            st.session_state["zillow_results_page"] = 1
                            st.rerun()
                    with bottom_cols[1]:
                        if st.button("◀️ Prev", key="zillow_page_prev_btm", disabled=current_page == 1):
                            st.session_state["zillow_results_page"] = current_page - 1
                            st.rerun()
                    with bottom_cols[2]:
                        st.markdown(f"**{current_page} / {total_pages}**")
                    with bottom_cols[3]:
                        if st.button("Next ▶️", key="zillow_page_next_btm", disabled=current_page == total_pages):
                            st.session_state["zillow_results_page"] = current_page + 1
                            st.rerun()
                    with bottom_cols[4]:
                        if st.button("Last ⏭️", key="zillow_page_last_btm", disabled=current_page == total_pages):
                            st.session_state["zillow_results_page"] = total_pages
                            st.rerun()
        
        elif st.session_state.get("zillow_progress") == 100:
            st.info("No properties found in the selected area. Try expanding the polygon or adjusting filters.")
        
        # Debug info
        with st.expander("🔧 Debug Info", expanded=False):
            st.json({
                "polygon_points": len(polygon_coords),
                "results_count": len(results),
                "last_query": st.session_state.get("zillow_last_query"),
                "status": st.session_state.get("zillow_status"),
                "progress": st.session_state.get("zillow_progress"),
                "error": st.session_state.get("zillow_error"),
                "api_metadata": st.session_state.get("zillow_api_metadata"),
            })
