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

from profile.ui import save_current_profile
from .chatbot import render_mortgage_chatbot


def _validate_table_rows(rows: pd.DataFrame, required_columns: list[str], table_label: str) -> list[str]:
    errors = []
    for idx, row in rows.iterrows():
        row_num = idx + 1
        for col in required_columns:
            value = row.get(col)
            if value is None or (isinstance(value, str) and not value.strip()):
                errors.append(f"{table_label} row {row_num}: '{col}' is required.")
        if "Amount" in rows.columns:
            amount = row.get("Amount")
            if amount is None or amount < 0:
                errors.append(f"{table_label} row {row_num}: 'Amount' must be >= 0.")
    return errors


def render_mortgage():
    if "mortgage_inputs" not in st.session_state:
        st.session_state["mortgage_inputs"] = {}
    if "mortgage_include_flags" not in st.session_state:
        st.session_state["mortgage_include_flags"] = {}

    mortgage_inputs = st.session_state["mortgage_inputs"]
    include_flags = st.session_state["mortgage_include_flags"]

    with st.expander(
            f"Mortgage & Loan Assumptions  •  {st.session_state['mortgage_badge']}",
            expanded=st.session_state["mortgage_expanded"],
    ):

        if "chart_visible" not in st.session_state:
            st.session_state["chart_visible"] = False

        # Layout: left input panel, right output panel
        left, right = st.columns([1.05, 1.25], gap="large")

        with left:
            st.subheader("House Purchase Essentials:")
            
            with st.expander("ℹ️ About this section", expanded=False):
                st.markdown("""
**Home Price** – The total purchase price of the property. This is the starting point for all mortgage calculations.

**Down Payment** – The upfront cash payment you make toward the home purchase:
- As **%**: Percentage of the home price (e.g., 20% of $400,000 = $80,000)
- As **$**: Fixed dollar amount

The **Loan Amount** = Home Price − Down Payment. A larger down payment reduces your loan amount and may help you avoid PMI (Private Mortgage Insurance).
""")

            home_price = st.number_input(
                "Home Price ($)",
                min_value=0.0,
                value=float(mortgage_inputs.get("home_price", 400000.0)),
                step=1000.0,
                format="%.2f"
            )

            st.caption("Down Payment")

            dp_cols = st.columns([0.75, 0.25], gap="small")
            with dp_cols[0]:
                down_payment_value = st.number_input(
                    "Down Payment",
                    min_value=0.0,
                    value=float(mortgage_inputs.get("down_payment_value", 20.0)),
                    step=1.0,
                    label_visibility="collapsed"
                )
            with dp_cols[1]:
                down_payment_is_percent_default = mortgage_inputs.get(
                    "down_payment_is_percent", True
                )
                down_payment_is_percent = st.selectbox(
                    "Unit",
                    ["%", "$"],
                    index=0 if down_payment_is_percent_default else 1,
                    label_visibility="collapsed"
                )

            dp_is_percent = (down_payment_is_percent == "%")

            # ---- House Purchase Essentials Validation Status ----
            purchase_errors = []
            if dp_is_percent:
                if down_payment_value < 0:
                    purchase_errors.append("Down payment percentage cannot be negative.")
                elif down_payment_value > 100:
                    purchase_errors.append("Down payment percentage cannot exceed 100%.")
            else:
                if down_payment_value < 0:
                    purchase_errors.append("Down payment amount cannot be negative.")
            
            if purchase_errors:
                st.error("\n".join([f"• {err}" for err in purchase_errors]))
            else:
                st.success("✓ House purchase inputs valid")

            st.markdown("#### Loan Terms")
            
            with st.expander("ℹ️ About loan terms", expanded=False):
                st.markdown("""
**Loan Term** – The number of years over which you'll repay the mortgage. Common terms are 15, 20, or 30 years. A shorter term means higher monthly payments but less total interest paid.

**Interest Rate** – The annual percentage rate (APR) charged by the lender. This rate is divided by 12 to calculate monthly interest. Even small rate differences significantly impact total interest paid over the life of the loan.

**Start Date** – When your first mortgage payment is due. This determines the payoff date calculation.
""")

            loan_term_years = st.number_input(
                "Loan Term (years)",
                min_value=1,
                value=int(mortgage_inputs.get("loan_term_years", 30)),
                step=1
            )

            annual_rate = st.number_input(
                "Interest Rate (%)",
                min_value=0.0,
                value=float(mortgage_inputs.get("annual_rate", 6.17)),
                step=0.01,
                format="%.2f"
            )

            # ---- Loan Terms Validation Status ----
            loan_errors = []
            if annual_rate < 0:
                loan_errors.append("Interest rate cannot be negative.")
            elif annual_rate > 100:
                loan_errors.append("Interest rate cannot exceed 100%.")
            
            if loan_errors:
                st.error("\n".join([f"• {err}" for err in loan_errors]))
            else:
                st.success("✓ Loan terms valid")

            sd_cols = st.columns([0.6, 0.4])
            with sd_cols[0]:
                start_month_default = mortgage_inputs.get("start_month", 1)
                start_month_name = st.selectbox(
                    "Start Date (month)",
                    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
                    index=max(0, min(11, int(start_month_default) - 1))
                )
            with sd_cols[1]:
                start_year = st.number_input(
                    "Start Date (year)",
                    min_value=1900,
                    max_value=2200,
                    value=int(mortgage_inputs.get("start_year", 2026)),
                    step=1
                )

            start_month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"].index(
                start_month_name) + 1

            st.markdown("### Annual Tax & Cost")
            
            with st.expander("ℹ️ About taxes & costs", expanded=False):
                st.markdown("""
**Property Tax** – Annual tax assessed by local government based on property value:
- As **%**: Percentage of home price (e.g., 1.12% of $400,000 = $4,480/year)
- As **$/year**: Fixed annual dollar amount

**Home Insurance** – Annual homeowners insurance premium. Protects against damage, theft, and liability.

**PMI (Private Mortgage Insurance)** – Monthly insurance required if your down payment is less than 20%. Protects the lender if you default.

**HOA Fee** – Monthly Homeowners Association fee for shared community amenities and maintenance (if applicable).

**Other Home Costs** – Annual budget for maintenance, repairs, lawn care, pest control, etc.

All costs are converted to monthly amounts and added to your total monthly payment.
""")

            include_costs = st.checkbox(
                "Include Taxes & Costs Below",
                value=bool(include_flags.get("include_costs", True))
            )

            tax_cols = st.columns([0.75, 0.25], gap="small")
            with tax_cols[0]:
                st.caption("Property Tax")
                property_tax_value = st.number_input(
                    "Property Taxes",
                    min_value=0.0,
                    value=float(mortgage_inputs.get("property_tax_value", 1.12)),
                    step=0.1,
                    label_visibility="collapsed"
                )
            with tax_cols[1]:
                st.caption("Unit")

                property_tax_is_percent_default = mortgage_inputs.get(
                    "property_tax_is_percent", True
                )
                property_tax_unit = st.selectbox(
                    "Unit",
                    ["%", "$/year"],
                    index=0 if property_tax_is_percent_default else 1,
                    label_visibility="collapsed"
                )

            property_tax_is_percent = (property_tax_unit == "%")

            home_insurance_annual = st.number_input(
                "Home Insurance ($/year)",
                min_value=0.0,
                value=float(mortgage_inputs.get("home_insurance_annual", 1500.0)),
                step=50.0
            )

            pmi_monthly = st.number_input(
                "PMI / Mortgage Insurance ($/month)",
                min_value=0.0,
                value=float(mortgage_inputs.get("pmi_monthly", 0.0)),
                step=10.0
            )

            hoa_monthly = st.number_input(
                "HOA Fee ($/month)",
                min_value=0.0,
                value=float(mortgage_inputs.get("hoa_monthly", 0.0)),
                step=10.0
            )

            other_yearly = st.number_input(
                "Other Home Costs ($/year)",
                min_value=0.0,
                value=float(mortgage_inputs.get("other_yearly", 0.0)),
                step=25.0,
                help="Home-related costs not captured above (maintenance, misc)."
            )

            # ---- Annual Tax & Cost Validation Status ----
            tax_cost_errors = []
            if property_tax_is_percent:
                if property_tax_value < 0:
                    tax_cost_errors.append("Property tax percentage cannot be negative.")
                elif property_tax_value > 100:
                    tax_cost_errors.append("Property tax percentage cannot exceed 100%.")
            else:
                if property_tax_value < 0:
                    tax_cost_errors.append("Property tax amount cannot be negative.")
            
            if tax_cost_errors:
                st.error("\n".join([f"• {err}" for err in tax_cost_errors]))
            else:
                st.success("✓ Tax & cost inputs valid")

            st.markdown("### Household Expenses")
            
            with st.expander("ℹ️ About household expenses", expanded=False):
                st.markdown("""
**Daycare** – Weekly childcare costs. Converted to monthly: (weekly × 52) ÷ 12.

**Groceries** – Weekly food and household supplies budget. Converted to monthly: (weekly × 52) ÷ 12.

**Utilities** – Monthly costs for electricity, gas, water, internet, phone, etc.

**Property Expenses** – Monthly property upkeep costs such as lawncare, maintenance, and repairs.

These expenses are **not part of the mortgage** but are included in your total monthly budget to help assess overall affordability.
""")

            include_household_expenses = st.checkbox(
                "Include Household Expenses Below",
                value=bool(include_flags.get("include_household_expenses", True))
            )

            daycare_weekly = st.number_input(
                "Daycare ($/week)",
                min_value=0.0,
                value=float(mortgage_inputs.get("daycare_weekly", 0.0)),
                step=50.0
            )

            groceries_weekly = st.number_input(
                "Groceries ($/week)",
                min_value=0.0,
                value=float(mortgage_inputs.get("groceries_weekly", 0.0)),
                step=10.0
            )

            utilities_monthly = st.number_input(
                "Utilities ($/month)",
                min_value=0.0,
                value=float(mortgage_inputs.get("utilities_monthly", 0.0)),
                step=25.0
            )

            property_expenses_monthly = st.number_input(
                "Property Expenses ($/month)",
                min_value=0.0,
                value=float(mortgage_inputs.get("property_expenses_monthly", 0.0)),
                step=25.0,
                help="Lawncare, maintenance, repairs, and other property upkeep costs."
            )

            household_monthly = (
                    (daycare_weekly * 52.0 / 12.0)
                    + (groceries_weekly * 52.0 / 12.0)
                    + utilities_monthly
                    + property_expenses_monthly
            ) if include_household_expenses else 0.0

            st.markdown("### Vehicle Expenses")
            
            with st.expander("ℹ️ About vehicle expenses", expanded=False):
                st.markdown("""
**Car Tax** – Annual vehicle registration/property tax. Converted to monthly: annual ÷ 12.

**Gasoline** – Weekly fuel costs based on your commute and driving habits. Converted to monthly: (weekly × 52) ÷ 12.

**Car Maintenance** – Annual budget for oil changes, tires, repairs, inspections. Converted to monthly: annual ÷ 12.

**Car Insurance** – Monthly auto insurance premium.

These expenses are **not part of the mortgage** but help assess your total monthly budget and affordability.
""")

            include_vehicle_expenses = st.checkbox(
                "Include Vehicle Expenses Below",
                value=bool(include_flags.get("include_vehicle_expenses", True))
            )

            car_tax_annual = st.number_input(
                "Car Tax ($/year)",
                min_value=0.0,
                value=float(mortgage_inputs.get("car_tax_annual", 1200.0)),
                step=50.0
            )

            vehicle_gas_weekly = st.number_input(
                "Gasoline ($/week)",
                min_value=0.0,
                value=float(mortgage_inputs.get("vehicle_gas_weekly", 0.0)),
                step=10.0
            )

            car_maintenance_annual = st.number_input(
                "Car Maintenance ($/year)",
                min_value=0.0,
                value=float(mortgage_inputs.get("car_maintenance_annual", 0.0)),
                step=100.0
            )

            car_insurance_monthly = st.number_input(
                "Car Insurance ($/month)",
                min_value=0.0,
                value=float(mortgage_inputs.get("car_insurance_monthly", 0.0)),
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
            
            with st.expander("ℹ️ About college savings", expanded=False):
                st.markdown("""
**529 Contribution** – Annual contribution per child to a 529 college savings plan. These are tax-advantaged accounts for education expenses.

**Number of Kids** – Multiply the annual contribution by the number of children.

**Monthly Calculation**: (Annual per child × Number of kids) ÷ 12

This expense is **not part of the mortgage** but helps you plan for future education costs while assessing home affordability.
""")

            include_college_savings = st.checkbox(
                "Include College Savings Below",
                value=bool(include_flags.get("include_college_savings", True))
            )

            college_cols = st.columns([0.7, 0.3], gap="small")

            with college_cols[0]:
                college_529_annual = st.number_input(
                    "529 Contribution ($/year per child)",
                    min_value=0.0,
                    value=float(mortgage_inputs.get("college_529_annual", 0.0)),
                    step=250.0
                )

            with college_cols[1]:
                num_kids = st.number_input(
                    "Kids",
                    min_value=1,
                    max_value=5,
                    value=int(mortgage_inputs.get("num_kids", 1)),
                    step=1
                )

            college_monthly = (
                    (college_529_annual * num_kids) / 12.0
            ) if include_college_savings else 0.0

            # =============================
            # Additional Custom Expenses
            # =============================
            st.markdown("### Additional Expenses")
            
            with st.expander("ℹ️ About additional expenses", expanded=False):
                st.markdown("""
Use the three inputs below to log recurring expenses not covered above.

**Fields:**
- **Expense** – A label/description for the expense (e.g., "Gym membership", "Streaming services")
- **Amount** – The dollar amount
- **Cadence** – Whether the amount is per month ($/month) or per year ($/year)

Each entry is saved into the scrollable log. Annual amounts are automatically converted to monthly.

Examples: subscriptions, memberships, student loans, childcare beyond daycare, pet expenses, etc.
""")

            include_custom_expenses = st.checkbox(
                "Include Additional Expenses Below",
                value=bool(include_flags.get("include_custom_expenses", True))
            )

            if "custom_expenses_log" not in st.session_state:
                st.session_state["custom_expenses_log"] = []

            if "custom_expense_cadence" not in st.session_state:
                st.session_state["custom_expense_cadence"] = "$/month"

            custom_cols = st.columns([1.1, 1.0, 0.6], gap="small")
            with custom_cols[0]:
                custom_label = st.text_input(
                    "Expense",
                    key="custom_expense_label",
                    placeholder="Gym, subscriptions, student loan",
                )
            with custom_cols[1]:
                custom_amount = st.number_input(
                    "Amount",
                    min_value=0.0,
                    step=10.0,
                    key="custom_expense_amount",
                )
            with custom_cols[2]:
                custom_cadence = st.selectbox(
                    "Cadence",
                    options=["$/month", "$/year"],
                    key="custom_expense_cadence",
                )

            def _add_custom_expense() -> None:
                label = st.session_state.get("custom_expense_label", "").strip()
                if not label:
                    st.session_state["custom_expense_error"] = "Please enter an expense label before adding."
                    return
                st.session_state.setdefault("custom_expenses_log", []).append(
                    {
                        "Label": label,
                        "Amount": st.session_state.get("custom_expense_amount", 0.0),
                        "Cadence": st.session_state.get("custom_expense_cadence", "$/month"),
                    }
                )
                st.session_state["custom_expense_label"] = ""
                st.session_state["custom_expense_amount"] = 0.0
                st.session_state["custom_expense_cadence"] = "$/month"
                st.session_state.pop("custom_expense_error", None)

            st.button("Add Expense", key="custom_expense_add", on_click=_add_custom_expense)
            if st.session_state.get("custom_expense_error"):
                st.warning(st.session_state["custom_expense_error"])

            def _save_custom_expense(idx: int) -> None:
                log = st.session_state.get("custom_expenses_log", [])
                if idx >= len(log):
                    return
                log[idx]["Label"] = st.session_state.get(f"custom_label_{idx}", "").strip()
                log[idx]["Amount"] = st.session_state.get(f"custom_amount_{idx}", 0.0)
                log[idx]["Cadence"] = st.session_state.get(f"custom_cadence_{idx}", "$/month")
                st.session_state["custom_expenses_log"] = log

            def _delete_custom_expense(idx: int) -> None:
                log = st.session_state.get("custom_expenses_log", [])
                if idx >= len(log):
                    return
                st.session_state["custom_expenses_log"] = [
                    row for i, row in enumerate(log) if i != idx
                ]

            custom_log_container = st.container(height=160)
            with custom_log_container:
                if not st.session_state["custom_expenses_log"]:
                    st.caption("No additional expenses logged yet.")
                else:
                    for idx, row in enumerate(st.session_state["custom_expenses_log"]):
                        row_cols = st.columns([1.2, 1.0, 0.7, 0.3, 0.3], gap="small")
                        with row_cols[0]:
                            label = st.text_input(
                                "Expense",
                                value=row.get("Label", ""),
                                key=f"custom_label_{idx}",
                                label_visibility="collapsed",
                            )
                        with row_cols[1]:
                            amount = st.number_input(
                                "Amount",
                                min_value=0.0,
                                step=10.0,
                                value=float(row.get("Amount", 0.0)),
                                key=f"custom_amount_{idx}",
                                label_visibility="collapsed",
                            )
                        with row_cols[2]:
                            cadence = st.selectbox(
                                "Cadence",
                                options=["$/month", "$/year"],
                                index=0 if row.get("Cadence") == "$/month" else 1,
                                key=f"custom_cadence_{idx}",
                                label_visibility="collapsed",
                            )
                        with row_cols[3]:
                            st.button(
                                "💾",
                                key=f"custom_save_{idx}",
                                on_click=_save_custom_expense,
                                args=(idx,),
                            )
                        with row_cols[4]:
                            st.button(
                                "🗑️",
                                key=f"custom_delete_{idx}",
                                on_click=_delete_custom_expense,
                                args=(idx,),
                            )

            # =============================
            # Take Home Pay
            # =============================
            st.markdown("### Take Home Pay")
            
            with st.expander("ℹ️ About take home pay", expanded=False):
                st.markdown("""
Use the three inputs below to log income sources for affordability comparison.

**Fields:**
- **Income Source** – A label for the income (e.g., "Salary - Partner 1", "Side gig")
- **Amount** – The dollar amount (after taxes)
- **Cadence** – Whether the amount is per month ($/month) or per year ($/year)

Each entry is saved into the scrollable log. Annual amounts are automatically converted to monthly.

**Affordability Check**: Your total monthly expenses (mortgage + taxes + all other costs) are compared against your total take-home pay. If expenses exceed income, a warning is displayed.

**Tip**: Enter your **net** (after-tax) income, not gross income.
""")

            include_take_home = st.checkbox(
                "Include Take Home Pay Comparison",
                value=bool(include_flags.get("include_take_home", True)),
            )

            # ---- Initialize backing data ----
            if "take_home_log" not in st.session_state:
                st.session_state["take_home_log"] = []

            if "take_home_cadence" not in st.session_state:
                st.session_state["take_home_cadence"] = "$/month"

            take_home_cols = st.columns([1.2, 1.0, 0.6], gap="small")
            with take_home_cols[0]:
                take_home_label = st.text_input(
                    "Income Source",
                    key="take_home_label",
                    placeholder="Salary, bonus, rental income",
                )
            with take_home_cols[1]:
                take_home_amount = st.number_input(
                    "Amount",
                    min_value=0.0,
                    step=100.0,
                    key="take_home_amount",
                )
            with take_home_cols[2]:
                take_home_cadence = st.selectbox(
                    "Cadence",
                    options=["$/month", "$/year"],
                    key="take_home_cadence",
                )

            def _add_take_home() -> None:
                label = st.session_state.get("take_home_label", "").strip()
                if not label:
                    st.session_state["take_home_error"] = "Please enter an income source label before adding."
                    return
                st.session_state.setdefault("take_home_log", []).append(
                    {
                        "Source": label,
                        "Amount": st.session_state.get("take_home_amount", 0.0),
                        "Cadence": st.session_state.get("take_home_cadence", "$/month"),
                    }
                )
                st.session_state["take_home_label"] = ""
                st.session_state["take_home_amount"] = 0.0
                st.session_state["take_home_cadence"] = "$/month"
                st.session_state.pop("take_home_error", None)

            st.button("Add Income", key="take_home_add", on_click=_add_take_home)
            if st.session_state.get("take_home_error"):
                st.warning(st.session_state["take_home_error"])

            def _save_take_home(idx: int) -> None:
                log = st.session_state.get("take_home_log", [])
                if idx >= len(log):
                    return
                log[idx]["Source"] = st.session_state.get(f"income_source_{idx}", "").strip()
                log[idx]["Amount"] = st.session_state.get(f"income_amount_{idx}", 0.0)
                log[idx]["Cadence"] = st.session_state.get(f"income_cadence_{idx}", "$/month")
                st.session_state["take_home_log"] = log

            def _delete_take_home(idx: int) -> None:
                log = st.session_state.get("take_home_log", [])
                if idx >= len(log):
                    return
                st.session_state["take_home_log"] = [
                    row for i, row in enumerate(log) if i != idx
                ]

            take_home_log_container = st.container(height=160)
            with take_home_log_container:
                if not st.session_state["take_home_log"]:
                    st.caption("No take-home income entries yet.")
                else:
                    for idx, row in enumerate(st.session_state["take_home_log"]):
                        row_cols = st.columns([1.2, 1.0, 0.7, 0.3, 0.3], gap="small")
                        with row_cols[0]:
                            source = st.text_input(
                                "Source",
                                value=row.get("Source", ""),
                                key=f"income_source_{idx}",
                                label_visibility="collapsed",
                            )
                        with row_cols[1]:
                            amount = st.number_input(
                                "Amount",
                                min_value=0.0,
                                step=100.0,
                                value=float(row.get("Amount", 0.0)),
                                key=f"income_amount_{idx}",
                                label_visibility="collapsed",
                            )
                        with row_cols[2]:
                            cadence = st.selectbox(
                                "Cadence",
                                options=["$/month", "$/year"],
                                index=0 if row.get("Cadence") == "$/month" else 1,
                                key=f"income_cadence_{idx}",
                                label_visibility="collapsed",
                            )
                        with row_cols[3]:
                            st.button(
                                "💾",
                                key=f"income_save_{idx}",
                                on_click=_save_take_home,
                                args=(idx,),
                            )
                        with row_cols[4]:
                            st.button(
                                "🗑️",
                                key=f"income_delete_{idx}",
                                on_click=_delete_take_home,
                                args=(idx,),
                            )

            st.session_state["mortgage_inputs"] = {
                "home_price": home_price,
                "down_payment_value": down_payment_value,
                "down_payment_is_percent": dp_is_percent,
                "loan_term_years": int(loan_term_years),
                "annual_rate": annual_rate,
                "start_month": int(start_month),
                "start_year": int(start_year),
                "property_tax_value": property_tax_value,
                "property_tax_is_percent": property_tax_is_percent,
                "home_insurance_annual": home_insurance_annual,
                "pmi_monthly": pmi_monthly,
                "hoa_monthly": hoa_monthly,
                "other_yearly": other_yearly,
                "daycare_weekly": daycare_weekly,
                "groceries_weekly": groceries_weekly,
                "utilities_monthly": utilities_monthly,
                "property_expenses_monthly": property_expenses_monthly,
                "car_tax_annual": car_tax_annual,
                "vehicle_gas_weekly": vehicle_gas_weekly,
                "car_maintenance_annual": car_maintenance_annual,
                "car_insurance_monthly": car_insurance_monthly,
                "college_529_annual": college_529_annual,
                "num_kids": num_kids,
            }
            st.session_state["mortgage_include_flags"] = {
                "include_costs": include_costs,
                "include_household_expenses": include_household_expenses,
                "include_vehicle_expenses": include_vehicle_expenses,
                "include_college_savings": include_college_savings,
                "include_custom_expenses": include_custom_expenses,
                "include_take_home": include_take_home,
            }
            st.session_state["custom_expenses_log"] = list(
                st.session_state.get("custom_expenses_log", [])
            )
            st.session_state["take_home_log"] = list(
                st.session_state.get("take_home_log", [])
            )

            custom_errors = []

            # =============================
            # Final Calculate Action
            # =============================
            
            # ---- Aggregate All Validation Errors ----
            all_validation_errors = (
                purchase_errors
                + loan_errors
                + tax_cost_errors
                + custom_errors
            )
            
            # ---- Final Status Bar for Errors ----
            error_placeholder = st.empty()
            if all_validation_errors:
                error_placeholder.error("**Cannot calculate:** Fix the errors above before proceeding.")
            else:
                error_placeholder.success("✓ Ready to calculate")
            
            with st.form("calculate_form"):
                calculate = st.form_submit_button("Calculate", type="primary", disabled=len(all_validation_errors) > 0)

            if calculate:
                st.session_state["mortgage_just_calculated"] = True
                st.session_state["chart_visible"] = True

            take_home_monthly = 0.0
            if include_take_home:
                for row in st.session_state.get("take_home_log", []):
                    if row.get("Cadence") == "$/month":
                        take_home_monthly += float(row.get("Amount", 0.0))
                    elif row.get("Cadence") == "$/year":
                        take_home_monthly += float(row.get("Amount", 0.0)) / 12.0

            render_mortgage_chatbot(
                inputs=MortgageInputs(
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
                    other_yearly=other_yearly,
                ),
                down_payment_amt=(home_price * (down_payment_value / 100.0))
                if dp_is_percent
                else down_payment_value,
                loan_amount=max(
                    home_price
                    - ((home_price * (down_payment_value / 100.0)) if dp_is_percent else down_payment_value),
                    0.0,
                ),
                include_take_home=include_take_home,
                take_home_monthly=take_home_monthly if include_take_home else None,
            )

            # Keep Mortgage section expanded after Calculate
            if calculate:
                st.session_state["mortgage_expanded"] = True

            if st.button("Save", key="save_mortgage_profile"):
                try:
                    save_path = save_current_profile()
                    st.success(f"Saved to {save_path}")
                except Exception as exc:
                    st.error(f"Save failed: {exc}")

            # Reset one-shot flag
            if st.session_state.pop("mortgage_just_calculated", False):
                st.session_state["mortgage_expanded"] = True

        # -----------------------------
        # CALCULATIONS (computed after inputs)
        # -----------------------------
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
        # (Visible only after Calculate - below the calculate button)
        # -----------------------------
        if st.session_state.get("chart_visible", False):
                # ---- Normalize Take Home (monthly) ----
                take_home_monthly = 0.0
                for row in st.session_state.get("take_home_log", []):
                    if row.get("Cadence") == "$/month":
                        take_home_monthly += float(row.get("Amount", 0.0))
                    elif row.get("Cadence") == "$/year":
                        take_home_monthly += float(row.get("Amount", 0.0)) / 12.0

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
                if include_custom_expenses:
                    for row in st.session_state.get("custom_expenses_log", []):
                        if row.get("Cadence") == "$/month":
                            custom_monthly += float(row.get("Amount", 0.0))
                        elif row.get("Cadence") == "$/year":
                            custom_monthly += float(row.get("Amount", 0.0)) / 12.0

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

                # ---- Display chart INSIDE expander ----
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
                        width='stretch'
                    )

                # ---- Display banner INSIDE expander ----
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

                # ---- Display summary INSIDE expander ----
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
