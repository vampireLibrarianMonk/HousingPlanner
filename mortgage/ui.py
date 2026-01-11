import streamlit as st
import pandas as pd

from .models import MortgageInputs
from .calculations import (
    monthly_pi_payment,
    amortization_totals,
    payoff_date,
)
from .costs import compute_costs_monthly


def render_bankrate_math():
    st.markdown("""
### Bankrate-Style Mortgage Calculation

**Monthly Principal & Interest**

\\[
M = P \times \frac{r(1+r)^n}{(1+r)^n - 1}
\\]

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

\\[
M = P \\times \\frac{r(1+r)^n}{(1+r)^n - 1}
\\]

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


def render_mortgage(method: str):
    with st.expander(
            f"Mortgage & Loan Assumptions  •  {st.session_state['mortgage_badge']}",
            expanded=st.session_state["mortgage_expanded"],
    ):

        with st.expander("Show the math & assumptions", expanded=False):
            if method == "Bankrate-style":
                render_bankrate_math()
            elif method == "NerdWallet-style":
                render_nerdwallet_math()

        # Layout: left input panel, right output panel
        left, right = st.columns([1.05, 1.25], gap="large")

        with left:
            with st.form("mortgage_form"):
                st.subheader("Modify the values and click Calculate")

                home_price = st.number_input(
                    "Home Price ($)",
                    min_value=0.0,
                    value=400000.0,
                    step=1000.0,
                    format="%.2f"
                )

                dp_cols = st.columns([0.75, 0.25], gap="small")
                with dp_cols[0]:
                    down_payment_value = st.number_input(
                        "Down Payment",
                        min_value=0.0,
                        value=20.0,
                        step=1.0,
                        label_visibility="collapsed"
                    )
                with dp_cols[1]:
                    down_payment_is_percent = st.selectbox(
                        "Unit",
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
                        ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
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

                start_month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov",
                               "Dec"].index(start_month_name) + 1

                include_costs = st.checkbox("Include Taxes & Costs Below", value=True)

                st.markdown("### Annual Tax & Cost")

                tax_cols = st.columns([0.75, 0.25], gap="small")
                with tax_cols[0]:
                    property_tax_value = st.number_input(
                        "Property Taxes",
                        min_value=0.0,
                        value=1.2,
                        step=0.1,
                        label_visibility="collapsed"
                    )
                with tax_cols[1]:
                    property_tax_unit = st.selectbox(
                        "Unit",
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
                    step=10.0
                )

                hoa_monthly = st.number_input(
                    "HOA Fee ($/month)",
                    min_value=0.0,
                    value=0.0,
                    step=10.0
                )

                other_monthly = st.number_input(
                    "Other Home Costs ($/year)",
                    min_value=0.0,
                    value=0.0,
                    step=25.0,
                    help="Home-related costs not captured above (maintenance, misc)."
                )

                # ---- Annual Tax & Cost Summary ----
                if property_tax_is_percent:
                    property_tax_annual = home_price * (property_tax_value / 100.0)
                else:
                    property_tax_annual = property_tax_value

                monthly_home_costs = (
                        property_tax_annual / 12.0
                        + home_insurance_annual / 12.0
                        + pmi_monthly
                        + hoa_monthly
                        + other_monthly / 12.0
                )

                include_household_expenses = st.checkbox(
                    "Include Household Expenses Below",
                    value=True
                )

                # =============================
                # Household Expenses
                # =============================
                st.markdown("### Household Expenses")

                if include_household_expenses:
                    daycare_monthly = st.number_input(
                        "Daycare ($/month)",
                        min_value=0.0,
                        value=0.0,
                        step=50.0
                    )

                    groceries_weekly = st.number_input(
                        "Groceries ($/week)",
                        min_value=0.0,
                        value=0.0,
                        step=10.0
                    )

                    utilities_monthly = st.number_input(
                        "Utilities ($/month)",
                        min_value=0.0,
                        value=0.0,
                        step=25.0
                    )

                    car_maintenance_annual = st.number_input(
                        "Car Maintenance ($/year)",
                        min_value=0.0,
                        value=0.0,
                        step=100.0
                    )
                else:
                    daycare_monthly = 0.0
                    groceries_weekly = 0.0
                    utilities_monthly = 0.0
                    car_maintenance_annual = 0.0

                # ---- Household Expenses Summary ----
                household_monthly = (
                        daycare_monthly
                        + (groceries_weekly * 52.0 / 12.0)
                        + utilities_monthly
                        + (car_maintenance_annual / 12.0)
                )

                calculate = st.form_submit_button("Calculate", type="primary")

                if calculate:
                    st.session_state["mortgage_expanded"] = True

            # =============================
            # Additional Custom Expenses
            # =============================
            st.markdown("### Additional Expenses")

            if "custom_expenses" not in st.session_state:
                st.session_state["custom_expenses"] = pd.DataFrame(
                    columns=["Label", "Amount", "Cadence"]
                )

            with st.form("custom_expenses_form"):
                custom_df = st.data_editor(
                    st.session_state["custom_expenses"],
                    hide_index=True,
                    num_rows="dynamic",
                    column_config={
                        "Label": st.column_config.TextColumn("Expense"),
                        "Amount": st.column_config.NumberColumn(
                            "Amount",
                            min_value=0.0,
                            step=10.0
                        ),
                        "Cadence": st.column_config.SelectboxColumn(
                            "Cadence",
                            options=["$/month", "$/year"]
                        ),
                    },
                )

                save_custom = st.form_submit_button("Apply Expenses")

            if save_custom:
                st.session_state["custom_expenses"] = custom_df

            if not st.session_state["custom_expenses"].empty:
                custom_monthly = 0.0
                for _, row in st.session_state["custom_expenses"].iterrows():
                    if row["Cadence"] == "$/month":
                        custom_monthly += row["Amount"]
                    elif row["Cadence"] == "$/year":
                        custom_monthly += row["Amount"] / 12.0

                st.caption(
                    f"**Custom Expenses Summary:** "
                    f"${custom_monthly:,.0f} / month"
                )

        # -----------------------------
        # RIGHT PANEL (computed outputs)
        # -----------------------------
        with right:
            if calculate:
                # ---- Loan Summary inputs ----
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
                        + household_monthly
                )

                # Green bar ONCE (after monthly_total exists)
                st.markdown(
                    f"""
                    <div style="padding: 14px; border-radius: 6px; background: #2e7d32;
                                color: white; font-size: 22px; font-weight: 700;">
                        Monthly Payment: ${monthly_total:,.2f}
                    </div>
                    """,
                    unsafe_allow_html=True
                )

                # Update badge once
                st.session_state["mortgage_badge"] = f"Monthly: ${monthly_total:,.0f}"

                # ---- Summary ----
                st.markdown("### Summary")
                payoff = payoff_date(
                    inputs.start_year,
                    inputs.start_month,
                    inputs.loan_term_years
                )

                custom_monthly = 0.0
                if not st.session_state.get("custom_expenses", pd.DataFrame()).empty:
                    for _, row in st.session_state["custom_expenses"].iterrows():
                        if row["Cadence"] == "$/month":
                            custom_monthly += row["Amount"]
                        elif row["Cadence"] == "$/year":
                            custom_monthly += row["Amount"] / 12.0

                c1, c2, c3 = st.columns(3)
                with c1:
                    st.metric("House Price", f"${home_price:,.2f}")
                    st.metric("Loan Amount", f"${loan_amount:,.2f}")
                    st.metric("Down Payment", f"${down_payment_amt:,.2f}")
                with c2:
                    st.metric("Total of Mortgage Payments (P&I)", f"${total_pi_paid:,.2f}")
                    st.metric("Total Interest", f"${total_interest:,.2f}")
                    st.metric("Mortgage Payoff Date", payoff)
                with c3:
                    st.metric(
                        "Tax & Cost (Monthly)",
                        f"${monthly_home_costs:,.0f}",
                        help="Property tax, insurance, HOA, PMI, other"
                    )
                    st.metric(
                        "Household Expenses (Monthly)",
                        f"${household_monthly:,.0f}",
                        help="Daycare, groceries, utilities, car"
                    )
                    st.metric(
                        "Additional Expenses (Monthly)",
                        f"${custom_monthly:,.0f}",
                        help="User-defined expenses (monthly + annual normalized)"
                    )