import time
from dataclasses import dataclass
from datetime import date, timezone

import polyline
import streamlit as st

import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim

import os
from dotenv import load_dotenv

import requests
import pandas as pd

from datetime import datetime, timedelta

# ---------------------------------------------
# Load environment variables (.env)
# ---------------------------------------------
load_dotenv()

# -----------------------------
# Calculation models
# -----------------------------
@dataclass(frozen=True)
class MortgageInputs:
    home_price: float
    down_payment_value: float
    down_payment_is_percent: bool
    loan_term_years: int
    annual_interest_rate_pct: float
    start_month: int
    start_year: int

    include_costs: bool

    # Taxes & costs (we keep all internally normalized to monthly dollars)
    property_tax_value: float
    property_tax_is_percent: bool  # if percent, percent of home price per year
    home_insurance_annual: float
    pmi_monthly: float
    hoa_monthly: float
    other_monthly: float


def monthly_pi_payment(principal: float, annual_rate_pct: float, term_years: int) -> float:
    """
    Standard fixed-rate amortization payment:
      M = P * [ r(1+r)^n / ((1+r)^n - 1) ]
    where r = annual_rate/12, n = years*12.

    Bankrate explicitly publishes this form and defines r as annual/12. :contentReference[oaicite:3]{index=3}
    """
    if principal <= 0:
        return 0.0
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0
    if r == 0:
        return principal / n
    num = r * (1 + r) ** n
    den = (1 + r) ** n - 1
    return principal * (num / den)


def amortization_totals(principal: float, annual_rate_pct: float, term_years: int, payment: float) -> tuple[float, float]:
    """
    Compute total interest and total paid (P+I) using a month-by-month schedule with cent rounding.
    This avoids drift and better matches what calculators display.
    """
    n = term_years * 12
    r = (annual_rate_pct / 100.0) / 12.0

    bal = principal
    total_interest = 0.0
    total_paid = 0.0

    for m in range(1, n + 1):
        if bal <= 0:
            break
        interest = round(bal * r, 2)
        principal_paid = round(payment - interest, 2)

        # If we're overpaying in the final month, clamp.
        if principal_paid > bal:
            principal_paid = round(bal, 2)
            payment_effective = round(principal_paid + interest, 2)
        else:
            payment_effective = round(payment, 2)

        bal = round(bal - principal_paid, 2)
        total_interest = round(total_interest + interest, 2)
        total_paid = round(total_paid + payment_effective, 2)

    return total_interest, total_paid


def compute_costs_monthly(inputs: MortgageInputs, method: str) -> dict:
    """
    Normalize costs to monthly amounts. Key difference between methods is *input cadence*.

    NerdWallet: tax & insurance are yearly; HOA & mortgage insurance are monthly. :contentReference[oaicite:4]{index=4}
    Bankrate: includes taxes/insurance/HOA in the monthly payment view; inputs are editable. :contentReference[oaicite:5]{index=5}
    """
    # Property tax monthly:
    if inputs.property_tax_is_percent:
        # percent of home price per year
        annual_tax = inputs.home_price * (inputs.property_tax_value / 100.0)
        property_tax_monthly = annual_tax / 12.0
    else:
        # dollar amount per year for both methods (we keep UI flexible)
        property_tax_monthly = inputs.property_tax_value / 12.0

    home_insurance_monthly = inputs.home_insurance_annual / 12.0

    # HOA and PMI handling:
    # Both Bankrate and NerdWallet treat HOA and PMI as monthly pass-through costs.
    # Differences between calculators are in input cadence and presentation, not math.
    hoa_monthly = inputs.hoa_monthly
    pmi_monthly = inputs.pmi_monthly

    other_monthly = inputs.other_monthly

    return {
        "property_tax_monthly": property_tax_monthly,
        "home_insurance_monthly": home_insurance_monthly,
        "hoa_monthly": hoa_monthly,
        "pmi_monthly": pmi_monthly,
        "other_monthly": other_monthly,
    }


def payoff_date(start_year: int, start_month: int, term_years: int) -> str:
    # payoff month is start + n-1 months (display only)
    n = term_years * 12
    y = start_year
    m = start_month
    m_total = (y * 12 + (m - 1)) + (n - 1)
    y2 = m_total // 12
    m2 = (m_total % 12) + 1
    return date(y2, m2, 1).strftime("%b. %Y")


def render_bankrate_math():
    st.markdown("""
### Bankrate-Style Mortgage Calculation

**Monthly Principal & Interest**

\[
M = P \times \frac{r(1+r)^n}{(1+r)^n - 1}
\]

Where:
- **P** = Loan principal  
- **r** = Annual interest rate ÷ 12  
- **n** = Loan term (years × 12)

**Assumptions**
- Fixed-rate mortgage
- Monthly compounding
- Cent-level rounding per payment
- Taxes, insurance, HOA added to monthly payment
- No ZIP-based tax estimation (user-supplied only)

**Notes**
- This matches Bankrate’s published amortization method.
- Extra payments supported in later phase.
""")


def render_nerdwallet_math():
    st.markdown("""
### NerdWallet-Style Mortgage Calculation

**Monthly Principal & Interest**

\[
M = P \\times \\frac{r(1+r)^n}{(1+r)^n - 1}
\]

Where:
- **P** = Loan principal  
- **r** = Annual interest rate ÷ 12  
- **n** = Loan term (years × 12)

**Assumptions**
- Fixed-rate mortgage
- Monthly compounding
- Cent-level rounding
- Property tax & homeowners insurance entered **annually**
- HOA & mortgage insurance entered **monthly**

**Notes**
- Matches NerdWallet’s cost-cadence behavior
- No automatic tax or insurance estimation
""")


def arm_delete(confirm_key):
    st.session_state[confirm_key] = True


def _get_loc_by_label(locations: list[dict], label: str) -> dict | None:
    for loc in locations:
        if loc["label"] == label:
            return loc
    return None


@st.cache_data(show_spinner=False)
def ors_directions_driving(
    api_key: str,
    start_lon: float,
    start_lat: float,
    end_lon: float,
    end_lat: float,
) -> tuple[float, float, list]:
    """
    Returns (distance_meters, duration_seconds) using ORS driving-car directions.
    Supports both ORS response formats and safely extracts metrics.
    """
    url = "https://api.openrouteservice.org/v2/directions/driving-car"
    headers = {
        "Content-Type": "application/json",
        "Authorization": api_key,
    }

    payload = {
        "coordinates": [
            [start_lon, start_lat],
            [end_lon, end_lat],
        ]
    }

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    # ---------------------------------------
    # Parse ORS response (features or routes)
    # ---------------------------------------

    # GeoJSON-style response
    if "features" in data and data["features"]:
        props = data["features"][0].get("properties", {})
        summary = props.get("summary")
        segments = props.get("segments")

    # Classic routing response
    elif "routes" in data and data["routes"]:
        route = data["routes"][0]
        summary = route.get("summary")
        segments = route.get("segments")

    else:
        err_msg = data.get("error", {}).get("message", str(data))
        raise RuntimeError(f"OpenRouteService error: {err_msg}")

    # ---------------------------------------
    # Extract distance & duration safely
    # ---------------------------------------

    distance = summary.get("distance") if summary else None
    duration = summary.get("duration") if summary else None

    # Fallback: some ORS responses only populate segments
    if (distance is None or duration is None) and segments:
        distance = segments[0].get("distance")
        duration = segments[0].get("duration")

    if distance is None or duration is None:
        raise RuntimeError(
            "OpenRouteService response missing distance/duration "
            f"(summary={summary}, segments={segments})"
        )

    geometry = None

    if "features" in data:
        geometry = data["features"][0]["geometry"]["coordinates"]
    elif "routes" in data:
        geometry = data["routes"][0].get("geometry")

    return float(distance), float(duration), geometry


@st.cache_data(show_spinner=False)
def google_directions_driving(
    api_key: str,
    start: dict,
    end: dict,
    departure_dt,
) -> tuple[float, float, list]:
    """
    Returns (distance_meters, duration_seconds) using Google Routes API.
    Traffic-aware when departure_dt is provided.
    """
    url = "https://routes.googleapis.com/directions/v2:computeRoutes"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": (
            "routes.duration,"
            "routes.distanceMeters,"
            "routes.polyline.encodedPolyline"
        ),
    }

    # ---------------------------------------
    # Ensure RFC3339 UTC timestamp (Z format)
    # ---------------------------------------
    if departure_dt.tzinfo is None:
        departure_dt = departure_dt.replace(tzinfo=timezone.utc)

    departure_ts = (
        departure_dt
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )

    payload = {
        "origin": {
            "location": {
                "latLng": {
                    "latitude": start["lat"],
                    "longitude": start["lon"],
                }
            }
        },
        "destination": {
            "location": {
                "latLng": {
                    "latitude": end["lat"],
                    "longitude": end["lon"],
                }
            }
        },
        "travelMode": "DRIVE",

        # Traffic-aware routing
        "routingPreference": "TRAFFIC_AWARE_OPTIMAL",
        "departureTime": departure_ts,

        # REQUIRED context
        "languageCode": "en-US",
        "units": "METRIC",

        # REQUIRED IN PRACTICE (even if all false)
        "routeModifiers": {
            "avoidTolls": False,
            "avoidHighways": False,
            "avoidFerries": False,
        },
    }

    # ---------------------------------------
    # DEBUG: inspect exact payload sent to Google
    # (TEMPORARY — remove after verification)
    # ---------------------------------------
    # st.code(payload, language="json")

    payload = {k: v for k, v in payload.items() if v is not None}

    resp = requests.post(url, headers=headers, json=payload, timeout=20)
    resp.raise_for_status()
    data = resp.json()

    route = data["routes"][0]

    # ---------------------------------------
    # Distance & duration may exist at
    # route-level OR leg-level (Google quirk)
    # ---------------------------------------

    distance = route.get("distanceMeters")
    duration = route.get("duration")

    # Fallback to first leg if needed
    if (distance is None or duration is None) and route.get("legs"):
        leg = route["legs"][0]
        distance = leg.get("distanceMeters")
        duration = leg.get("duration")

    if distance is None or duration is None:
        raise RuntimeError(
            "Google Routes response missing distance/duration: "
            f"{route}"
        )

    polyline = route.get("polyline", {}).get("encodedPolyline")

    return (
        float(distance),
        float(duration.rstrip("s")),
        polyline,
    )


def geocode_once(address: str) -> tuple[float, float]:
    geolocator = Nominatim(
        user_agent="house-planner-prototype",
        timeout=5,
    )
    location = geolocator.geocode(address)
    if not location:
        raise RuntimeError(f"Could not geocode address: {address}")
    return location.latitude, location.longitude


def decode_geometry(geometry, provider):
    """
    Returns list of (lat, lon)
    """
    if not geometry:
        return []

    if provider == "ORS":
        # ORS may return encoded polyline OR GeoJSON coordinates
        if isinstance(geometry, str):
            return polyline.decode(geometry)

        # GeoJSON-style [[lon, lat], ...]
        return [(lat, lon) for lon, lat in geometry]

    if provider == "GOOGLE":
        return polyline.decode(geometry)

    return []


# -----------------------------
# Session State
# -----------------------------
if "map_data" not in st.session_state:
    default_locations = [
        {
            "label": "House",
            "address": "4005 Ancient Oak Ct, Annandale, VA 22003",
        },
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

if "map_badge" not in st.session_state:
    st.session_state["map_badge"] = "3 locations"

if "map_expanded" not in st.session_state:
    st.session_state["map_expanded"] = False

if "commute_results" not in st.session_state:
    # Holds results per provider: {"ORS": {...}, "Google": {...}}
    st.session_state["commute_results"] = {}

if "commute_expanded" not in st.session_state:
    st.session_state["commute_expanded"] = True

if "show_ors" not in st.session_state:
    st.session_state["show_ors"] = False

if "show_google" not in st.session_state:
    st.session_state["show_google"] = False

if "show_markers" not in st.session_state:
    st.session_state["show_markers"] = False

# -----------------------------
# Streamlit UI
# -----------------------------
st.set_page_config(page_title="House Planner (Prototype)", layout="wide")

st.title("House Planner (Prototype)")

method = st.selectbox(
    "Calculation method",
    ["Bankrate-style", "NerdWallet-style"],
    help="Affects input conventions and displayed assumptions."
)

if "mortgage_badge" not in st.session_state:
    st.session_state["mortgage_badge"] = "Monthly: —"

# -----------------------------
# Safe defaults for section badges
# -----------------------------
monthly_badge = "Monthly: —"
map_badge = "0 locations"
commute_badge = "—"

# =============================
# Mortgage Section
# =============================
with st.expander(
    f"Mortgage & Loan Assumptions  •  {st.session_state['mortgage_badge']}",
    expanded=True
):

    with st.expander("Show the math & assumptions", expanded=False):
        if method == "Bankrate-style":
            render_bankrate_math()
        elif method == "NerdWallet-style":
            render_nerdwallet_math()

    # Layout: left input panel, right output panel
    left, right = st.columns([1.05, 1.25], gap="large")

    with left:
        st.subheader("Modify the values and click Calculate")

        home_price = st.number_input(
            "Home Price ($)",
            min_value=0.0,
            value=400000.0,
            step=1000.0,
            format="%.2f"
        )

        dp_cols = st.columns([0.6, 0.4])
        with dp_cols[0]:
            down_payment_value = st.number_input(
                "Down Payment",
                min_value=0.0,
                value=20.0,
                step=1.0
            )
        with dp_cols[1]:
            down_payment_is_percent = st.selectbox(
                "Down Payment Unit",
                ["%", "$"],
                index=0,
                label_visibility="collapsed"
            )
        dp_is_percent = (down_payment_is_percent == "%")

        loan_term_years = st.number_input(
            "Loan Term (years)",
            min_value=1,
            value=30,
            step=1
        )

        annual_rate = st.number_input(
            "Interest Rate (%)",
            min_value=0.0,
            value=6.17,
            step=0.01,
            format="%.2f"
        )

        sd_cols = st.columns([0.6, 0.4])
        with sd_cols[0]:
            start_month_name = st.selectbox(
                "Start Date (month)",
                ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],
                index=0
            )
        with sd_cols[1]:
            start_year = st.number_input(
                "Start Date (year)",
                min_value=1900,
                max_value=2200,
                value=2026,
                step=1
            )

        start_month = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"].index(start_month_name) + 1

        include_costs = st.checkbox("Include Taxes & Costs Below", value=True)

        st.markdown("### Annual Tax & Cost")

        tax_cols = st.columns([0.6, 0.4])
        with tax_cols[0]:
            property_tax_value = st.number_input(
                "Property Taxes",
                min_value=0.0,
                value=1.2,
                step=0.1
            )
        with tax_cols[1]:
            property_tax_unit = st.selectbox(
                "Property Tax Unit",
                ["%", "$/year"],
                index=0,
                label_visibility="collapsed"
            )

        property_tax_is_percent = (property_tax_unit == "%")

        home_insurance_annual = st.number_input(
            "Home Insurance ($/year)",
            min_value=0.0,
            value=1500.0,
            step=50.0
        )

        pmi_monthly = st.number_input(
            "PMI / Mortgage Insurance ($/month)",
            min_value=0.0,
            value=0.0,
            step=10.0,
            help="Optional. Set to 0 if not applicable."
        )

        hoa_monthly = st.number_input(
            "HOA Fee ($/month)",
            min_value=0.0,
            value=0.0,
            step=10.0
        )

        other_monthly = st.number_input(
            "Other Costs ($/month)",
            min_value=0.0,
            value=333.33,
            step=10.0
        )

        calculate = st.button("Calculate", type="primary")

    # -----------------------------
    # RIGHT PANEL (computed outputs)
    # -----------------------------
    with right:
        # Down payment & loan amount
        if dp_is_percent:
            down_payment_amt = home_price * (down_payment_value / 100.0)
        else:
            down_payment_amt = down_payment_value

        loan_amount = max(home_price - down_payment_amt, 0.0)

        inputs = MortgageInputs(
            home_price=home_price,
            down_payment_value=down_payment_value,
            down_payment_is_percent=dp_is_percent,
            loan_term_years=int(loan_term_years),
            annual_interest_rate_pct=annual_rate,
            start_month=int(start_month),
            start_year=int(start_year),
            include_costs=include_costs,
            property_tax_value=property_tax_value,
            property_tax_is_percent=property_tax_is_percent,
            home_insurance_annual=home_insurance_annual,
            pmi_monthly=pmi_monthly,
            hoa_monthly=hoa_monthly,
            other_monthly=other_monthly,
        )

        pi = monthly_pi_payment(
            loan_amount,
            inputs.annual_interest_rate_pct,
            inputs.loan_term_years
        )

        total_interest, total_pi_paid = amortization_totals(
            loan_amount,
            inputs.annual_interest_rate_pct,
            inputs.loan_term_years,
            pi
        )

        costs = compute_costs_monthly(inputs, method=method)

        monthly_tax = costs["property_tax_monthly"] if include_costs else 0.0
        monthly_ins = costs["home_insurance_monthly"] if include_costs else 0.0
        monthly_hoa = costs["hoa_monthly"] if include_costs else 0.0
        monthly_pmi = costs["pmi_monthly"] if include_costs else 0.0
        monthly_other = costs["other_monthly"] if include_costs else 0.0

        monthly_total = (
            pi
            + monthly_tax
            + monthly_ins
            + monthly_hoa
            + monthly_pmi
            + monthly_other
        )

        # Update badge *after* calculation
        st.session_state["mortgage_badge"] = f"Monthly: ${monthly_total:,.0f}"

        st.markdown(
            f"""
            <div style="padding: 14px; border-radius: 6px; background: #2e7d32;
                        color: white; font-size: 22px; font-weight: 700;">
                Monthly Pay: ${monthly_total:,.2f}
            </div>
            """,
            unsafe_allow_html=True
        )

        # ---- Summary (unchanged, now fully wired) ----
        st.markdown("### Summary")
        payoff = payoff_date(
            inputs.start_year,
            inputs.start_month,
            inputs.loan_term_years
        )

        c1, c2 = st.columns(2)
        with c1:
            st.metric("House Price", f"${home_price:,.2f}")
            st.metric("Loan Amount", f"${loan_amount:,.2f}")
            st.metric("Down Payment", f"${down_payment_amt:,.2f}")
        with c2:
            st.metric("Total of Mortgage Payments (P&I)", f"${total_pi_paid:,.2f}")
            st.metric("Total Interest", f"${total_interest:,.2f}")
            st.metric("Mortgage Payoff Date", payoff)

# =============================
# Map Section
# =============================
with st.expander(
    f"Map & Locations  •  {st.session_state['map_badge']}",
    expanded=st.session_state["map_expanded"]
):
    st.subheader("Add a Location")

    geolocator = Nominatim(user_agent="house-planner-prototype")

    # -----------------------------
    # Add-location form (keeps UI stable)
    # -----------------------------
    with st.form("add_location_form"):
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
                location = geolocator.geocode(location_address)
                if location:
                    st.session_state["map_data"]["locations"].append({
                        "label": location_label,
                        "address": location_address,
                        "lat": location.latitude,
                        "lon": location.longitude,
                    })

                    # Update badge and keep section open
                    count = len(st.session_state["map_data"]["locations"])
                    st.session_state["map_badge"] = f"{count} locations"
                    st.session_state["map_expanded"] = True
                else:
                    st.error("Address not found. Try a more complete address.")
            except Exception as e:
                st.error(f"Geocoding error: {e}")

    # -----------------------------
    # Build and render map + table
    # -----------------------------
    locations = st.session_state["map_data"]["locations"]

    map_col, table_col = st.columns([0.7, 0.3], gap="large")

    # -------- MAP COLUMN --------
    with map_col:
        # ---------------------------------------
        # Apply auto-defaults ONCE, then release control
        # ---------------------------------------
        if st.session_state.get("last_commute_provider_applied") is not True:
            provider = st.session_state.get("last_commute_provider")

            if provider == "ORS":
                st.session_state["show_ors"] = True
                st.session_state["show_google"] = False
            elif provider == "Google":
                st.session_state["show_google"] = True
                st.session_state["show_ors"] = False

            if st.session_state.get("auto_show_markers"):
                st.session_state["show_markers"] = True
                st.session_state.pop("auto_show_markers", None)

            # mark defaults as applied so user regains control
            st.session_state["last_commute_provider_applied"] = True

        show_ors = st.checkbox("Show ORS routes", key="show_ors")
        show_google = st.checkbox("Show Google routes", key="show_google")
        show_markers = st.checkbox("Show depart / arrive markers", key="show_markers")

        m = folium.Map(
            location=[39.8283, -98.5795],
            zoom_start=4,
            tiles="OpenStreetMap"
        )

        bounds = []

        for idx, loc in enumerate(locations):
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

        # Draw commute route if available
        if st.session_state.get("commute_results"):
            all_results = st.session_state.get("commute_results", {})

            for provider, res in all_results.items():
                segment_routes = res.get("segment_routes", [])

                for seg in segment_routes:
                    pts = seg["points"]
                    if not pts:
                        continue

                    # Decide visibility per provider (ROUTES ONLY)
                    show_route = (
                            (provider == "ORS" and show_ors)
                            or (provider == "Google" and show_google)
                    )

                    line_color = "#5E35B1" if provider == "ORS" else "#00695C"
                    prefix = f"{provider}: "

                    # --------------------
                    # Route line
                    # --------------------
                    if show_route:
                        folium.PolyLine(
                            locations=pts,
                            color=line_color,
                            weight=5,
                            opacity=0.85,
                            tooltip=f"{prefix}{seg['from']} → {seg['to']}"
                        ).add_to(m)

                    # --------------------
                    # Markers (independent)
                    # --------------------
                    if show_markers:
                        # FROM marker
                        folium.CircleMarker(
                            location=pts[0],
                            radius=6,
                            color="#1565C0",
                            fill=True,
                            fill_color="#1565C0",
                            fill_opacity=1.0,
                            tooltip=f"{prefix}Depart: {seg['from']}",
                            z_index_offset=1000,
                        ).add_to(m)

                        # TO marker
                        folium.CircleMarker(
                            location=pts[-1],
                            radius=6,
                            color="#C62828",
                            fill=True,
                            fill_color="#C62828",
                            fill_opacity=1.0,
                            tooltip=f"{prefix}Arrive: {seg['to']}",
                            z_index_offset=1000,
                        ).add_to(m)

        st_folium(m, width=900, height=500)

        # -----------------------------
        # Dynamic route legend
        # -----------------------------
        legend_lines = ["<b>Route Legend</b><br>"]

        if show_ors:
            legend_lines.append(
                "<span style='color:#5E35B1;'>━</span> ORS route (average traffic)<br>"
            )

        if show_google:
            legend_lines.append(
                "<span style='color:#00695C;'>━</span> Google route (traffic-aware)<br>"
            )

        if show_markers:
            legend_lines.append(
                "<span style='color:#1565C0;'>●</span> Depart &nbsp;&nbsp;"
                "<span style='color:#C62828;'>●</span> Arrive"
            )

        st.markdown(
            f"""
        <div style="margin-top:8px; font-size:14px;">
        {''.join(legend_lines)}
        </div>
        """,
            unsafe_allow_html=True
        )

    # -------- TABLE COLUMN --------
    with table_col:
        st.subheader("Locations")

        if not locations:
            st.caption("No locations added yet.")
        else:
            # Table header
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

                # ---- Label ----
                with row_cols[0]:
                    st.write(loc["label"])

                # ---- Address (single line, clipped) ----
                with row_cols[1]:
                    st.caption(loc["address"])

                # ---- Delete / Confirm ----
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
                        if st.session_state[confirm_key]:
                            if st.button(
                                    "Confirm",
                                    key=f"confirm_{idx}",
                                    type="primary"
                            ):
                                st.session_state["map_data"]["locations"].pop(idx)
                                st.session_state.pop(confirm_key, None)

                                count = len(st.session_state["map_data"]["locations"])
                                st.session_state["map_badge"] = f"{count} locations"
                                st.session_state["map_expanded"] = True

                                st.rerun()

# =============================
# Commute Section
# =============================
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
        st.stop()

    # ---------------------------------------------
    # Routing Method Selection
    # ---------------------------------------------
    routing_method = st.selectbox(
        "Routing Method",
        ["OpenRouteService (average traffic)", "Google (traffic-aware)"],
        help="Choose between average traffic (ORS) or traffic-aware routing (Google)."
    )

    # ---------------------------------------------
    # Routing API Keys (loaded from .env)
    # ---------------------------------------------
    ors_api_key = os.getenv("ORS_API_KEY")
    google_api_key = os.getenv("GOOGLE_MAPS_API_KEY")

    if routing_method.startswith("OpenRouteService"):
        if not ors_api_key:
            st.error(
                "ORS_API_KEY is not set. "
                "Add it to the .env file in the project root."
            )
            st.stop()
    else:
        if not google_api_key:
            st.error(
                "GOOGLE_MAPS_API_KEY is not set. "
                "Add it to the .env file in the project root."
            )
            st.stop()

    # ---------------------------------------------
    # Departure Time (used for traffic-aware routing)
    # ---------------------------------------------
    departure_time = st.time_input(
        "Departure Time (from Home)",
        value=pd.to_datetime("07:45").time(),
        help="Used for traffic-aware routing (Google only)."
    )

    # --- Choose Home of Record ---
    labels = [l["label"] for l in locations]
    home_label = st.selectbox(
        "Home of Record (start/end)",
        options=labels,
        index=labels.index("House") if "House" in labels else 0
    )
    home = _get_loc_by_label(locations, home_label)
    if not home:
        st.error("Home of record not found.")
        st.stop()

    # --- Editable table to choose stops + set order ---
    if "commute_table" not in st.session_state:
        # Initialize with everything excluded except non-home locations
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

    # -------------------------------------------------
    # One-time sync of commute table with locations
    # (DO NOT mutate after editor is rendered)
    # -------------------------------------------------
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

    # -------------------------------------------------
    # Build itinerary (ordered stops with optional revisit)
    # -------------------------------------------------

    # Only consider rows explicitly marked Include = True
    stops_df = edited[edited["Include"] == True].copy()
    if stops_df.empty:
        st.info(
            "Select at least one stop (Include = true), "
            "then set Order (1, 2, 3...)."
        )
        st.stop()

    # Sort stops in visit order (deterministic)
    # Order is primary; Label breaks ties
    stops_df.sort_values(["Order", "Label"], inplace=True)

    # Resolve stops into location objects
    ordered_locs = []
    revisit_locs = []
    missing = []

    for _, row in stops_df.iterrows():
        label = row["Label"]

        # Look up the location from the Map section
        loc = _get_loc_by_label(locations, label)
        if not loc:
            missing.append(label)
            continue

        # First visit (always)
        ordered_locs.append(loc)

        # Optional second visit (e.g., daycare pickup)
        if row.get("Revisit", False):
            revisit_locs.append(loc)

    # Fail fast if any labels could not be resolved
    if missing:
        st.error(
            "These stops are missing from Map locations: "
            + ", ".join(missing)
        )
        st.stop()

    # -------------------------------------------------
    # Trigger route computation (persist results)
    # -------------------------------------------------
    compute = st.button("Compute Commute", type="primary")

    if compute:
        # -------------------------------------------------
        # Prepare route accumulation
        # -------------------------------------------------
        seg_rows = []
        total_m = 0.0  # meters
        total_s = 0.0  # seconds

        # Accumulate decoded route geometry
        all_route_points = []

        # Store per-segment routes for coloring
        segment_routes = []

        # Final route:
        #   Home → ordered stops → revisited stops → Home
        #
        # Example:
        #   Home → Daycare → Work → Daycare → Home
        points = [home] + ordered_locs + revisit_locs + [home]

        # -------------------------------------------------
        # Compute route segments via selected routing method
        # -------------------------------------------------
        spinner_label = (
            "Computing route segments (ORS)…"
            if routing_method.startswith("OpenRouteService")
            else "Computing route segments (Google, traffic-aware)…"
        )

        # # Initialize clock at departure time
        # today = pd.Timestamp.today().date()
        # current_dt = datetime.combine(today, departure_time)

        # Google traffic-aware routing only supports near-term departures
        now = datetime.now(tz=timezone.utc)

        candidate_dt = now.replace(
            hour=departure_time.hour,
            minute=departure_time.minute,
            second=0,
            microsecond=0,
        )

        # Google Routes REQUIRES departureTime >= now
        if candidate_dt <= now:
            candidate_dt = now + timedelta(minutes=1)

        current_dt = candidate_dt

        with st.spinner(spinner_label):
            for i in range(len(points) - 1):
                a = points[i]
                b = points[i + 1]

                if routing_method.startswith("OpenRouteService"):
                    dist_m, dur_s, geom = ors_directions_driving(
                        api_key=ors_api_key,
                        start_lon=a["lon"], start_lat=a["lat"],
                        end_lon=b["lon"], end_lat=b["lat"],
                    )

                    pts = decode_geometry(geom, "ORS")
                else:
                    dist_m, dur_s, geom = google_directions_driving(
                        api_key=google_api_key,
                        start=a,
                        end=b,
                        departure_dt=current_dt,
                    )

                    pts = decode_geometry(geom, "GOOGLE")

                if all_route_points and pts:
                    all_route_points.extend(pts[1:])  # avoid duplicate join point
                else:
                    all_route_points.extend(pts)

                # Persist this leg for map rendering
                segment_routes.append({
                    "from": a["label"],
                    "to": b["label"],
                    "points": pts,
                    "provider": (
                        "ORS"
                        if routing_method.startswith("OpenRouteService")
                        else "Google"
                    ),
                })

                arrive_dt = current_dt + timedelta(seconds=dur_s)

                # Loiter applies at destination (if defined)
                loiter_min = 0
                match = stops_df[stops_df["Label"] == b["label"]]
                if not match.empty:
                    loiter_min = int(match.iloc[0].get("Loiter (min)", 0))

                leave_dt = arrive_dt + timedelta(minutes=loiter_min)

                total_m += dist_m
                total_s += dur_s + (loiter_min * 60)

                seg_rows.append({
                    "From": a["label"],
                    "To": b["label"],
                    "Depart": current_dt.strftime("%H:%M"),
                    "Arrive": arrive_dt.strftime("%H:%M"),
                    "Drive (min)": round(dur_s / 60.0, 1),
                    "Loiter (min)": loiter_min,
                    "Leave": leave_dt.strftime("%H:%M"),
                    "Cumulative (min)": round(total_s / 60.0, 1),
                })

                # Advance clock
                current_dt = leave_dt

        # -------------------------------------------------
        # Persist results for re-render on rerun
        # -------------------------------------------------
        provider_key = (
            "ORS"
            if routing_method.startswith("OpenRouteService")
            else "Google"
        )

        st.session_state["commute_results"][provider_key] = {
            "segments": pd.DataFrame(seg_rows),
            "total_m": total_m,
            "total_s": total_s,
            "segment_routes": segment_routes,
        }

        # ---------------------------------------
        # Record desired layer defaults for NEXT rerun
        # ---------------------------------------
        st.session_state["last_commute_provider"] = provider_key
        st.session_state["last_commute_provider_applied"] = False
        st.session_state["auto_show_markers"] = True

        # UI focus
        st.session_state["commute_expanded"] = False
        st.session_state["map_expanded"] = True

    # -------------------------------------------------
    # Display persisted results (if available)
    # -------------------------------------------------
    if st.session_state.get("commute_results"):
        # Show results for the most recently computed provider
        provider_key = st.session_state.get("last_commute_provider")

        res = (
            st.session_state.get("commute_results", {})
            .get(provider_key)
        )

        if res:
            st.subheader(f"{provider_key} Commute Results")

            st.dataframe(
                res["segments"],
                width="stretch",
                hide_index=True
            )

            st.markdown("### Totals")
            st.metric(
                "Total Distance",
                f"{res['total_m'] / 1609.344:,.2f} mi"
            )
            st.metric(
                "Total Drive Time",
                f"{res['total_s'] / 60.0:,.1f} min"
            )
