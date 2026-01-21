import streamlit as st
import pandas as pd

from .models import MortgageInputs
from .calculations import (
    monthly_pi_payment,
    amortization_totals,
    amortization_schedule,
    amortization_schedule_with_extra,
    payoff_date,
)

from .costs import compute_costs_monthly

import matplotlib.pyplot as plt

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

        if "chart_visible" not in st.session_state:
            st.session_state["chart_visible"] = False

        with st.expander("Show the math & assumptions", expanded=False):
            if method == "Bankrate-style":
                render_bankrate_math()
            elif method == "NerdWallet-style":
                render_nerdwallet_math()

        # Layout: left input panel, right output panel
        left, right = st.columns([1.05, 1.25], gap="large")

        with left:
            st.subheader("House Purchase Essentials:")

            home_price = st.number_input(
                "Home Price ($)",
                min_value=0.0,
                value=400000.0,
                step=1000.0,
                format="%.2f"
            )

            st.caption("Down Payment")

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

            start_month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"].index(
                start_month_name) + 1

            st.markdown("### Annual Tax & Cost")

            include_costs = st.checkbox(
                "Include Taxes & Costs Below",
                value=True
            )

            tax_cols = st.columns([0.75, 0.25], gap="small")
            with tax_cols[0]:
                st.caption("Property Tax")
                property_tax_value = st.number_input(
                    "Property Taxes",
                    min_value=0.0,
                    value=1.12,
                    step=0.1,
                    label_visibility="collapsed"
                )
            with tax_cols[1]:
                st.caption("Unit")

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

            other_yearly = st.number_input(
                "Other Home Costs ($/year)",
                min_value=0.0,
                value=0.0,
                step=25.0,
                help="Home-related costs not captured above (maintenance, misc)."
            )

            st.markdown("### Household Expenses")

            include_household_expenses = st.checkbox(
                "Include Household Expenses Below",
                value=True
            )

            daycare_weekly = st.number_input(
                "Daycare ($/week)",
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

            household_monthly = (
                    (daycare_weekly * 52.0 / 12.0)
                    + (groceries_weekly * 52.0 / 12.0)
                    + utilities_monthly
            ) if include_household_expenses else 0.0

            st.markdown("### Vehicle Expenses")

            include_vehicle_expenses = st.checkbox(
                "Include Vehicle Expenses Below",
                value=True
            )

            car_tax_annual = st.number_input(
                "Car Tax ($/year)",
                min_value=0.0,
                value=1200.0,
                step=50.0
            )

            vehicle_gas_weekly = st.number_input(
                "Gasoline ($/week)",
                min_value=0.0,
                value=0.0,
                step=10.0
            )

            car_maintenance_annual = st.number_input(
                "Car Maintenance ($/year)",
                min_value=0.0,
                value=0.0,
                step=100.0
            )

            car_insurance_monthly = st.number_input(
                "Car Insurance ($/month)",
                min_value=0.0,
                value=0.0,
                step=25.0
            )

            vehicle_monthly = (
                    (car_tax_annual / 12.0)
                    + (vehicle_gas_weekly * 52.0 / 12.0)
                    + (car_maintenance_annual / 12.0)
                    + car_insurance_monthly
            ) if include_vehicle_expenses else 0.0

            # =============================
            # Kids College Savings
            # =============================
            st.markdown("### Kids College Savings")

            include_college_savings = st.checkbox(
                "Include College Savings Below",
                value=True
            )

            college_cols = st.columns([0.7, 0.3], gap="small")

            with college_cols[0]:
                college_529_annual = st.number_input(
                    "529 Contribution ($/year per child)",
                    min_value=0.0,
                    value=0.0,
                    step=250.0
                )

            with college_cols[1]:
                num_kids = st.number_input(
                    "Kids",
                    min_value=1,
                    max_value=5,
                    value=1,
                    step=1
                )

            college_monthly = (
                    (college_529_annual * num_kids) / 12.0
            ) if include_college_savings else 0.0

            # =============================
            # Additional Custom Expenses
            # =============================
            st.markdown("### Additional Expenses")

            include_custom_expenses = st.checkbox(
                "Include Additional Expenses Below",
                value=True
            )

            # ---- Initialize backing data (NON-widget key) ----
            if "custom_expenses_df" not in st.session_state:
                st.session_state["custom_expenses_df"] = pd.DataFrame(
                    columns=["Label", "Amount", "Cadence"]
                )

            # ---- Widget owns its own key ----
            custom_df = st.session_state["custom_expenses_df"].reset_index(drop=True)

            custom_expenses_editor = st.data_editor(
                custom_df,
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
                key="custom_expenses_editor",
            )

            # =============================
            # Take Home Pay
            # =============================
            st.markdown("### Take Home Pay")

            include_take_home = st.checkbox(
                "Include Take Home Pay Comparison",
                value=True,
            )

            # ---- Initialize backing data ----
            if "take_home_sources_df" not in st.session_state:
                st.session_state["take_home_sources_df"] = pd.DataFrame(
                    columns=["Source", "Amount", "Cadence"]
                )

            take_home_df = st.session_state["take_home_sources_df"].reset_index(drop=True)

            take_home_editor = st.data_editor(
                take_home_df,
                hide_index=True,
                num_rows="dynamic",
                column_config={
                    "Source": st.column_config.TextColumn("Income Source"),
                    "Amount": st.column_config.NumberColumn(
                        "Amount",
                        min_value=0.0,
                        step=100.0
                    ),
                    "Cadence": st.column_config.SelectboxColumn(
                        "Cadence",
                        options=["$/month", "$/year"]
                    ),
                },
                key="take_home_editor",
            )

            # =============================
            # Final Calculate Action
            # =============================
            with st.form("calculate_form"):
                calculate = st.form_submit_button("Calculate", type="primary")

            if calculate:
                st.session_state["mortgage_just_calculated"] = True
                st.session_state["chart_visible"] = True

            # ---- Commit table edits ONLY on Calculate ----
            if calculate:
                st.session_state["custom_expenses_df"] = (
                    pd.DataFrame(custom_expenses_editor, columns=["Label", "Amount", "Cadence"])
                    .reset_index(drop=True)
                )

                st.session_state["take_home_sources_df"] = (
                    pd.DataFrame(take_home_editor, columns=["Source", "Amount", "Cadence"])
                    .reset_index(drop=True)
                )

                # Keep Mortgage section expanded after Calculate
                st.session_state["mortgage_expanded"] = True

            # Reset one-shot flag
            if st.session_state.pop("mortgage_just_calculated", False):
                st.session_state["mortgage_expanded"] = True

        # -----------------------------
        # RIGHT PANEL (computed outputs)
        # -----------------------------
        with right:
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
                other_yearly=other_yearly
            )

            pi = monthly_pi_payment(
                loan_amount,
                inputs.annual_interest_rate_pct,
                inputs.loan_term_years
            )

            # -----------------------------
            # Amortization Chart + Controls
            # (Visible only after Calculate)
            # -----------------------------
            if st.session_state.get("chart_visible", False):
                # ---- Normalize Take Home (monthly) ----
                take_home_monthly = 0.0
                if not st.session_state.get("take_home_sources_df", pd.DataFrame()).empty:
                    for _, row in st.session_state["take_home_sources_df"].iterrows():
                        if row["Cadence"] == "$/month":
                            take_home_monthly += row["Amount"]
                        elif row["Cadence"] == "$/year":
                            take_home_monthly += row["Amount"] / 12.0

                total_interest, total_pi_paid = amortization_totals(
                    loan_amount,
                    inputs.annual_interest_rate_pct,
                    inputs.loan_term_years,
                    pi
                )

                costs = compute_costs_monthly(inputs)

                monthly_tax = costs["property_tax_monthly"] if include_costs else 0.0
                monthly_ins = costs["home_insurance_monthly"] if include_costs else 0.0
                monthly_hoa = costs["hoa_monthly"] if include_costs else 0.0
                monthly_pmi = costs["pmi_monthly"] if include_costs else 0.0
                monthly_other = costs["other_home_monthly"] if include_costs else 0.0

                custom_monthly = 0.0
                if include_custom_expenses and not st.session_state.get("custom_expenses_df", pd.DataFrame()).empty:
                    for _, row in st.session_state["custom_expenses_df"].iterrows():
                        if row["Cadence"] == "$/month":
                            custom_monthly += row["Amount"]
                        elif row["Cadence"] == "$/year":
                            custom_monthly += row["Amount"] / 12.0

                monthly_total = (
                        pi
                        + monthly_tax
                        + monthly_ins
                        + monthly_hoa
                        + monthly_pmi
                        + monthly_other
                        + household_monthly
                        + vehicle_monthly
                        + college_monthly
                        + custom_monthly
                )

                effective_take_home = take_home_monthly if include_take_home else None

                is_affordable = (
                        effective_take_home is not None
                        and monthly_total <= effective_take_home
                )

                leftover_monthly = (
                    take_home_monthly - monthly_total
                    if include_take_home
                    else None
                )

                payment_color = "#2e7d32" if is_affordable or not include_take_home else "#c62828"

                banner_parts = [
                    f"Monthly Payment: ${monthly_total:,.2f}"
                ]

                if include_take_home:
                    banner_parts.append(f"Take Home Pay: ${take_home_monthly:,.0f}")
                    banner_parts.append(
                        f"Leftover: ${leftover_monthly:,.0f}"
                        if leftover_monthly is not None
                        else "Leftover: N/A"
                    )

                banner_text = "  |  ".join(banner_parts)

                tax_cost_monthly = monthly_tax + monthly_ins + monthly_hoa + monthly_pmi + monthly_other
                payoff = payoff_date(
                    inputs.start_year,
                    inputs.start_month,
                    inputs.loan_term_years
                )

                # Store all data for display outside expander
                st.session_state["_mortgage_banner"] = {
                    "include_take_home": include_take_home,
                    "is_affordable": is_affordable,
                    "banner_text": banner_text,
                    "payment_color": payment_color,
                    "monthly_total": monthly_total,
                }

                st.session_state["_mortgage_summary"] = {
                    "home_price": home_price,
                    "loan_amount": loan_amount,
                    "down_payment_amt": down_payment_amt,
                    "total_pi_paid": total_pi_paid,
                    "total_interest": total_interest,
                    "payoff": payoff,
                    "pi": pi,
                    "tax_cost_monthly": tax_cost_monthly,
                    "household_monthly": household_monthly,
                    "vehicle_monthly": vehicle_monthly,
                    "college_monthly": college_monthly,
                    "custom_monthly": custom_monthly,
                }

                st.session_state["_mortgage_chart"] = {
                    "loan_amount": loan_amount,
                    "annual_rate": inputs.annual_interest_rate_pct,
                    "loan_term_years": inputs.loan_term_years,
                    "pi": pi,
                }

    # ---- Display chart OUTSIDE expander ----
    if "_mortgage_chart" in st.session_state and st.session_state.get("chart_visible", False):
        chart_data = st.session_state["_mortgage_chart"]
        
        # ---- Extra Payment Controls STATE ----
        extra_payment_amount = st.session_state.get("extra_payment_amount", 0.0)
        extra_payment_freq = st.session_state.get("extra_payment_freq", 1)

        if "use_extra_payments" not in st.session_state:
            st.session_state["use_extra_payments"] = False

        if st.session_state.get("redo_chart", False):
            st.session_state["use_extra_payments"] = True

        # ---- Select amortization for chart ----
        if (
                st.session_state.get("use_extra_payments", False)
                and extra_payment_amount > 0
        ):
            amortization_df = amortization_schedule_with_extra(
                chart_data["loan_amount"],
                chart_data["annual_rate"],
                chart_data["loan_term_years"],
                chart_data["pi"],
                extra_payment_annual=extra_payment_amount * extra_payment_freq,
            )
        else:
            amortization_df = amortization_schedule(
                chart_data["loan_amount"],
                chart_data["annual_rate"],
                chart_data["loan_term_years"],
                chart_data["pi"]
            )

        years = amortization_df.index + 1
        interest_cumulative = amortization_df["Interest"].cumsum()
        balance = amortization_df["Ending Balance"]
        annual_payment = amortization_df["Interest"] + amortization_df["Principal"]

        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot(years, balance, label="Remaining Balance", linewidth=2)
        ax.plot(years, interest_cumulative, label="Cumulative Interest", linewidth=2)
        ax.plot(years, annual_payment, label="Annual Payment", linewidth=2, linestyle="--")

        ax.set_title("Mortgage Amortization Over Time")
        ax.set_xlabel("Year")
        ax.set_ylabel("Dollars")
        ax.grid(True, linestyle="--", alpha=0.6)
        ax.legend()

        st.pyplot(fig)

        controls_row = st.columns([1, 2, 2, 2, 1], gap="small")

        with controls_row[1]:
            st.number_input(
                "Extra Payment ($)",
                min_value=0.0,
                step=100.0,
                key="extra_payment_amount",
                label_visibility="collapsed",
            )
            st.caption("Extra Payment ($)")

        with controls_row[2]:
            st.number_input(
                "Times / Year",
                min_value=1,
                step=1,
                key="extra_payment_freq",
                label_visibility="collapsed",
            )
            st.caption("Times / Year")

        with controls_row[3]:
            st.button(
                "Redo Chart",
                key="redo_chart",
                use_container_width=True
            )

    # ---- Display banner OUTSIDE expander (below the Mortgage section) ----
    if "_mortgage_banner" in st.session_state:
        banner = st.session_state["_mortgage_banner"]

        if banner["include_take_home"]:
            affordability_sentence = (
                "✅ This purchase is affordable for you."
                if banner["is_affordable"]
                else "⚠️ This purchase exceeds your take-home pay."
            )
            st.markdown(f"**{affordability_sentence}**")

        st.markdown(
            f"""
            <div style="
                padding: 14px;
                border-radius: 6px;
                background: {banner['payment_color']};
                color: white;
                font-size: 22px;
                font-weight: 700;
            ">
                {banner['banner_text']}
            </div>
            """,
            unsafe_allow_html=True
        )

        # Update badge
        st.session_state["mortgage_badge"] = f"Monthly: ${banner['monthly_total']:,.0f}"

    # ---- Display summary OUTSIDE expander ----
    if "_mortgage_summary" in st.session_state and st.session_state.get("chart_visible", False):
        summary = st.session_state["_mortgage_summary"]
        
        st.markdown("### Summary")

        c1, c2 = st.columns(2)
        with c1:
            st.metric("House Price", f"${summary['home_price']:,.2f}")
            st.metric("Loan Amount", f"${summary['loan_amount']:,.2f}")
            st.metric("Down Payment", f"${summary['down_payment_amt']:,.2f}")
            st.metric("Total of Mortgage Payments (P&I)", f"${summary['total_pi_paid']:,.2f}")
            st.metric("Total Interest", f"${summary['total_interest']:,.2f}")
            st.metric("Mortgage Payoff Date", summary['payoff'])
        with c2:
            st.metric(
                "Mortgage (Monthly)",
                f"${summary['pi']:,.0f}",
                help="Principal & Interest only"
            )
            st.metric(
                "Tax & Cost (Monthly)",
                f"${summary['tax_cost_monthly']:,.0f}",
                help="Property tax, insurance, HOA, PMI, other (monthly normalized)"
            )
            st.metric(
                "Household Expenses (Monthly)",
                f"${summary['household_monthly']:,.0f}",
                help="Daycare, groceries, utilities, car"
            )
            st.metric(
                "Vehicle Expenses (Monthly)",
                f"${summary['vehicle_monthly']:,.0f}",
                help="Car tax (annual) + Gasoline (weekly) + Maintenance (annual) + Insurance (monthly)"
            )
            st.metric(
                "College Savings (Monthly)",
                f"${summary['college_monthly']:,.0f}",
                help="529 contribution per child × number of kids (annual → monthly)"
            )
            st.metric(
                "Additional Expenses (Monthly)",
                f"${summary['custom_monthly']:,.0f}",
                help="User-defined expenses (monthly + annual normalized)"
            )
