from dataclasses import dataclass
from datetime import date
import streamlit as st


import folium
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim

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

if "map_data" not in st.session_state:
    st.session_state["map_data"] = {
        "locations": []
    }

if "map_badge" not in st.session_state:
    st.session_state["map_badge"] = "0 locations"

if "map_expanded" not in st.session_state:
    st.session_state["map_expanded"] = False

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

        st_folium(m, width=900, height=500)

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
# Commute Section (placeholder)
# =============================
with st.expander(f"Commute Analysis  •  {commute_badge}", expanded=False):
    st.info("Commute time and distance analysis will appear here.")
