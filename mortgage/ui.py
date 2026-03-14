import streamlit as st
import pandas as pd
from typing import Any
import tempfile
import os
import json
import hashlib
from pathlib import Path

from fpdf import FPDF

from .models import MortgageInputs
from .calculations import (
    monthly_pi_payment,
    amortization_totals,
    amortization_totals_with_adjustments,
    amortization_schedule,
    amortization_schedule_with_adjustments,
    payoff_date,
    payoff_date_from_months,
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


def _fmt_currency(value: float) -> str:
    return f"${value:,.2f}"


def _pdf_text(value: Any) -> str:
    return str(value).encode("latin-1", "replace").decode("latin-1")


def _monthly_total_from_log(
        rows: list[dict[str, Any]],
        amount_key: str = "Amount",
        cadence_key: str = "Cadence",
) -> float:
    total = 0.0
    for row in rows:
        amount = float(row.get(amount_key, 0.0) or 0.0)
        cadence = row.get(cadence_key, "$/month")
        if cadence == "$/year":
            total += amount / 12.0
        else:
            total += amount
    return total


def _build_mortgage_assumptions_pdf(report: dict[str, Any]) -> bytes:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=12)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Mortgage & Loan Assumptions Plan", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 7, "Generated from current Mortgage section inputs", ln=True)
    pdf.ln(2)

    def add_section(title: str, rows: list[tuple[str, str]]) -> None:
        pdf.set_font("Helvetica", "B", 12)
        pdf.cell(0, 8, _pdf_text(title), ln=True)
        pdf.set_font("Helvetica", "", 10)
        for label, value in rows:
            pdf.multi_cell(0, 6, _pdf_text(f"- {label}: {value}"))
        pdf.ln(1)

    add_section("1) House Purchase Essentials", report["house_purchase"])
    add_section("2) Loan Terms", report["loan_terms"])
    add_section("3) Annual Tax & Cost", report["tax_cost"])
    add_section("4) Household Expenses", report["household"])
    add_section("5) Vehicle Expenses", report["vehicle"])
    add_section("6) Kids College Savings", report["college"])

    add_section("7) Additional Expenses", report["custom_expenses"])
    if report["custom_expense_rows"]:
        pdf.set_font("Helvetica", "", 10)
        for row in report["custom_expense_rows"]:
            pdf.multi_cell(
                0,
                6,
                _pdf_text(
                    f"  - {row.get('Label', '')}: {_fmt_currency(float(row.get('Amount', 0.0) or 0.0))} ({row.get('Cadence', '$/month')})"
                ),
            )
        pdf.ln(1)

    add_section("8) Take Home Pay", report["take_home"])
    if report["take_home_rows"]:
        pdf.set_font("Helvetica", "", 10)
        for row in report["take_home_rows"]:
            pdf.multi_cell(
                0,
                6,
                _pdf_text(
                    f"  - {row.get('Source', '')}: {_fmt_currency(float(row.get('Amount', 0.0) or 0.0))} ({row.get('Cadence', '$/month')})"
                ),
            )
        pdf.ln(1)

    add_section("9) Extra Principal Payments", report["extra_principal"])
    if report["lump_sum_rows"]:
        pdf.set_font("Helvetica", "", 10)
        for row in report["lump_sum_rows"]:
            pdf.multi_cell(
                0,
                6,
                _pdf_text(
                    f"  - Year {int(row.get('Year', 1))}: {_fmt_currency(float(row.get('Amount', 0.0) or 0.0))}"
                ),
            )
        pdf.ln(1)

    chart_payload = report.get("chart_payload")
    chart_path = report.get("chart_image_path")
    generated_temp_chart = False
    if chart_path and not os.path.exists(str(chart_path)):
        chart_path = None
    if chart_payload and not chart_path:
        try:
            chart_path = _build_amortization_chart_image(chart_payload)
            generated_temp_chart = bool(chart_path)
            if chart_path:
                pdf.set_font("Helvetica", "B", 11)
                pdf.cell(0, 8, _pdf_text("Amortization Graphic"), ln=True)
                pdf.image(chart_path, w=185)
                pdf.ln(2)
        except Exception:
            # Keep PDF generation resilient if chart rendering fails.
            pass
        finally:
            if generated_temp_chart and chart_path and os.path.exists(chart_path):
                os.remove(chart_path)
    elif chart_path:
        pdf.set_font("Helvetica", "B", 11)
        pdf.cell(0, 8, _pdf_text("Amortization Graphic"), ln=True)
        pdf.image(str(chart_path), w=185)
        pdf.ln(2)

    add_section("10) Summary & Decision Metrics", report["summary"])

    output = pdf.output(dest="S")
    if isinstance(output, (bytes, bytearray)):
        return bytes(output)
    return output.encode("latin-1")


def _build_amortization_chart_image(chart_payload: dict[str, Any]) -> str | None:
    return _build_amortization_chart_image_to_path(chart_payload, output_path=None)


def _compute_amortization_from_payload(chart_payload: dict[str, Any]) -> dict[str, Any]:
    loan_amount = float(chart_payload.get("loan_amount", 0.0) or 0.0)
    annual_rate = float(chart_payload.get("annual_rate", 0.0) or 0.0)
    loan_term_years = int(chart_payload.get("loan_term_years", 30) or 30)
    pi = float(chart_payload.get("pi", 0.0) or 0.0)

    recurring_extra_amount = float(chart_payload.get("recurring_extra_amount", 0.0) or 0.0)
    recurring_frequency_months = int(chart_payload.get("recurring_frequency_months", 1) or 1)
    recurring_start_month = int(chart_payload.get("recurring_start_month", 1) or 1)
    recurring_end_month = int(chart_payload.get("recurring_end_month", loan_term_years * 12) or (loan_term_years * 12))
    start_year = int(chart_payload.get("start_year", 2026) or 2026)
    start_month = int(chart_payload.get("start_month", 1) or 1)

    lump_sum_rows = list(chart_payload.get("lump_sum_rows", []))
    lump_sum_by_month = {}
    for row in lump_sum_rows:
        year_num = max(1, min(loan_term_years, int(row.get("Year", 1))))
        amount = max(0.0, float(row.get("Amount", 0.0) or 0.0))
        month_index = (year_num - 1) * 12 + 1
        lump_sum_by_month[month_index] = lump_sum_by_month.get(month_index, 0.0) + amount

    has_recurring = recurring_extra_amount > 0
    has_lumps = any(float(row.get("Amount", 0.0) or 0.0) > 0 for row in lump_sum_rows)

    if has_recurring or has_lumps:
        dynamic_total_interest, dynamic_total_pi_paid, dynamic_months_to_payoff = amortization_totals_with_adjustments(
            loan_amount,
            annual_rate,
            loan_term_years,
            pi,
            recurring_extra_amount=recurring_extra_amount,
            recurring_frequency_months=recurring_frequency_months,
            recurring_start_month=recurring_start_month,
            recurring_end_month=recurring_end_month,
            lump_sum_by_month=lump_sum_by_month,
        )
        dynamic_payoff = payoff_date_from_months(
            start_year,
            start_month,
            dynamic_months_to_payoff,
        )
        amortization_df = amortization_schedule_with_adjustments(
            loan_amount,
            annual_rate,
            loan_term_years,
            pi,
            recurring_extra_amount=recurring_extra_amount,
            recurring_frequency_months=recurring_frequency_months,
            recurring_start_month=recurring_start_month,
            recurring_end_month=recurring_end_month,
            lump_sum_by_month=lump_sum_by_month,
        )
    else:
        dynamic_total_interest, dynamic_total_pi_paid = amortization_totals(
            loan_amount,
            annual_rate,
            loan_term_years,
            pi,
        )
        dynamic_payoff = payoff_date(start_year, start_month, loan_term_years)
        amortization_df = amortization_schedule(
            loan_amount,
            annual_rate,
            loan_term_years,
            pi,
        )

    return {
        "loan_amount": loan_amount,
        "annual_rate": annual_rate,
        "loan_term_years": loan_term_years,
        "pi": pi,
        "dynamic_total_interest": dynamic_total_interest,
        "dynamic_total_pi_paid": dynamic_total_pi_paid,
        "dynamic_payoff": dynamic_payoff,
        "amortization_df": amortization_df,
        "has_recurring": has_recurring,
        "has_lumps": has_lumps,
    }


def _build_amortization_chart_image_to_path(chart_payload: dict[str, Any], output_path: str | None = None) -> str | None:
    amortization_info = _compute_amortization_from_payload(chart_payload)
    amortization_df = amortization_info["amortization_df"]
    dynamic_total_interest = float(amortization_info["dynamic_total_interest"])
    dynamic_total_pi_paid = float(amortization_info["dynamic_total_pi_paid"])
    dynamic_payoff = str(amortization_info["dynamic_payoff"])

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

    chart_summary_text = (
        f"Total of Mortgage Payments (P&I): { _fmt_currency(dynamic_total_pi_paid)}\n"
        f"Total Interest: { _fmt_currency(dynamic_total_interest)}\n"
        f"Mortgage Payoff Date: {dynamic_payoff}"
    )
    ax.text(
        0.98,
        0.98,
        chart_summary_text,
        transform=ax.transAxes,
        fontsize=9.5,
        va="top",
        ha="right",
        bbox={
            "boxstyle": "round,pad=0.4",
            "facecolor": "white",
            "edgecolor": "#9e9e9e",
            "alpha": 0.9,
        },
    )

    if output_path:
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(output), format="png", dpi=150, bbox_inches="tight")
        tmp_path = str(output)
    else:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            fig.savefig(tmp.name, format="png", dpi=150, bbox_inches="tight")
            tmp_path = tmp.name

    plt.close(fig)
    return tmp_path


def _amortization_cache_image_path(chart_payload: dict[str, Any]) -> Path:
    base_dir = Path("data/cache/amortization")

    owner_sub = str(st.session_state.get("profile_owner_sub", "local"))
    house_slug = str(st.session_state.get("profile_house_slug", "unscoped-house"))

    payload_str = json.dumps(chart_payload, sort_keys=True, default=str)
    digest = hashlib.sha1(payload_str.encode("utf-8")).hexdigest()[:12]

    file_name = f"amortization_{owner_sub}_{house_slug}_{digest}.png"
    safe_name = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in file_name)
    return base_dir / safe_name


def _ensure_amortization_chart_cached(chart_payload: dict[str, Any]) -> str | None:
    target = _amortization_cache_image_path(chart_payload)
    if not target.exists():
        try:
            _build_amortization_chart_image_to_path(chart_payload, output_path=str(target))
        except Exception:
            return None
    return str(target)


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

**Closing Costs** – Buyer-side costs due at closing (title/escrow, lender fees, etc.):
- As **%**: Percentage of the home price
- As **$**: Fixed dollar amount

**Earnest Money** – Deposit paid when the offer is accepted (credited at closing):
- As **%**: Percentage of the home price
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

            st.caption("Closing Costs")

            cc_cols = st.columns([0.75, 0.25], gap="small")
            with cc_cols[0]:
                closing_costs_value = st.number_input(
                    "Closing Costs",
                    min_value=0.0,
                    value=float(mortgage_inputs.get("closing_costs_value", 0.0)),
                    step=500.0,
                    label_visibility="collapsed"
                )
            with cc_cols[1]:
                closing_costs_is_percent_default = mortgage_inputs.get(
                    "closing_costs_is_percent", False
                )
                closing_costs_is_percent = st.selectbox(
                    "Unit",
                    ["%", "$"],
                    index=0 if closing_costs_is_percent_default else 1,
                    key="closing_costs_unit",
                    label_visibility="collapsed"
                )

            cc_is_percent = (closing_costs_is_percent == "%")

            st.caption("Earnest Money")
            st.caption("ℹ️ Earnest money is credited toward your down payment at closing.")

            em_cols = st.columns([0.75, 0.25], gap="small")
            with em_cols[0]:
                earnest_money_value = st.number_input(
                    "Earnest Money",
                    min_value=0.0,
                    value=float(mortgage_inputs.get("earnest_money_value", 0.0)),
                    step=500.0,
                    label_visibility="collapsed"
                )
            with em_cols[1]:
                earnest_money_is_percent_default = mortgage_inputs.get(
                    "earnest_money_is_percent", False
                )
                earnest_money_is_percent = st.selectbox(
                    "Unit",
                    ["%", "$"],
                    index=0 if earnest_money_is_percent_default else 1,
                    key="earnest_money_unit",
                    label_visibility="collapsed"
                )

            em_is_percent = (earnest_money_is_percent == "%")

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

            if cc_is_percent:
                if closing_costs_value < 0:
                    purchase_errors.append("Closing costs percentage cannot be negative.")
                elif closing_costs_value > 100:
                    purchase_errors.append("Closing costs percentage cannot exceed 100%.")
            else:
                if closing_costs_value < 0:
                    purchase_errors.append("Closing costs amount cannot be negative.")

            if em_is_percent:
                if earnest_money_value < 0:
                    purchase_errors.append("Earnest money percentage cannot be negative.")
                elif earnest_money_value > 100:
                    purchase_errors.append("Earnest money percentage cannot exceed 100%.")
            else:
                if earnest_money_value < 0:
                    purchase_errors.append("Earnest money amount cannot be negative.")
            
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
                "closing_costs_value": closing_costs_value,
                "closing_costs_is_percent": cc_is_percent,
                "earnest_money_value": earnest_money_value,
                "earnest_money_is_percent": em_is_percent,
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
                    closing_costs_value=closing_costs_value,
                    closing_costs_is_percent=cc_is_percent,
                    earnest_money_value=earnest_money_value,
                    earnest_money_is_percent=em_is_percent,
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

            if st.button("Generate PDF", key="generate_mortgage_pdf"):
                try:
                    down_payment_amt_pdf = (
                        home_price * (down_payment_value / 100.0)
                        if dp_is_percent
                        else down_payment_value
                    )
                    loan_amount_pdf = max(home_price - down_payment_amt_pdf, 0.0)

                    inputs_pdf = MortgageInputs(
                        home_price=home_price,
                        down_payment_value=down_payment_value,
                        down_payment_is_percent=dp_is_percent,
                        closing_costs_value=closing_costs_value,
                        closing_costs_is_percent=cc_is_percent,
                        earnest_money_value=earnest_money_value,
                        earnest_money_is_percent=em_is_percent,
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
                    )
                    costs_pdf = compute_costs_monthly(inputs_pdf)
                    pi_pdf = monthly_pi_payment(
                        loan_amount_pdf,
                        annual_rate,
                        int(loan_term_years),
                    )
                    payoff_pdf = payoff_date(
                        int(start_year),
                        int(start_month),
                        int(loan_term_years),
                    )
                    monthly_interest_payment_pdf = (
                        loan_amount_pdf * ((annual_rate / 100.0) / 12.0)
                        if loan_amount_pdf > 0 and annual_rate > 0
                        else 0.0
                    )

                    custom_rows = list(st.session_state.get("custom_expenses_log", []))
                    take_home_rows = list(st.session_state.get("take_home_log", []))
                    lump_sum_rows = list(st.session_state.get("lump_sum_payments", []))

                    custom_monthly_pdf = (
                        _monthly_total_from_log(custom_rows)
                        if include_custom_expenses
                        else 0.0
                    )
                    take_home_monthly_pdf = (
                        _monthly_total_from_log(take_home_rows)
                        if include_take_home
                        else 0.0
                    )

                    monthly_tax_pdf = costs_pdf["property_tax_monthly"] if include_costs else 0.0
                    monthly_ins_pdf = costs_pdf["home_insurance_monthly"] if include_costs else 0.0
                    monthly_hoa_pdf = costs_pdf["hoa_monthly"] if include_costs else 0.0
                    monthly_pmi_pdf = costs_pdf["pmi_monthly"] if include_costs else 0.0
                    monthly_other_pdf = costs_pdf["other_home_monthly"] if include_costs else 0.0

                    monthly_total_pdf = (
                            pi_pdf
                            + monthly_tax_pdf
                            + monthly_ins_pdf
                            + monthly_hoa_pdf
                            + monthly_pmi_pdf
                            + monthly_other_pdf
                            + household_monthly
                            + vehicle_monthly
                            + college_monthly
                            + custom_monthly_pdf
                    )

                    is_affordable_pdf = (
                            include_take_home
                            and monthly_total_pdf <= take_home_monthly_pdf
                    )
                    leftover_monthly_pdf = (
                        take_home_monthly_pdf - monthly_total_pdf
                        if include_take_home
                        else None
                    )

                    chart_payload_pdf = {
                        "loan_amount": loan_amount_pdf,
                        "annual_rate": annual_rate,
                        "loan_term_years": int(loan_term_years),
                        "pi": pi_pdf,
                        "recurring_extra_amount": float(st.session_state.get("scheduled_extra_amount", 0.0) or 0.0),
                        "recurring_frequency_months": {
                            "Monthly": 1,
                            "Quarterly": 3,
                            "Semi-Annual": 6,
                            "Annual": 12,
                        }.get(str(st.session_state.get("scheduled_extra_frequency", "Monthly")), 1),
                        "recurring_start_month": (
                            (int(st.session_state.get("scheduled_extra_start_year", 1) or 1) - 1) * 12
                        ) + 1,
                        "recurring_end_month": int(st.session_state.get("scheduled_extra_end_year", int(loan_term_years)) or int(loan_term_years)) * 12,
                        "lump_sum_rows": lump_sum_rows,
                        "start_year": int(start_year),
                        "start_month": int(start_month),
                    }

                    # Keep PDF summary metrics aligned with on-screen amortization details.
                    try:
                        amortization_info_pdf = _compute_amortization_from_payload(chart_payload_pdf)
                        summary_total_pi_paid_pdf = float(amortization_info_pdf["dynamic_total_pi_paid"])
                        summary_total_interest_pdf = float(amortization_info_pdf["dynamic_total_interest"])
                        summary_payoff_pdf = str(amortization_info_pdf["dynamic_payoff"])
                    except Exception:
                        summary_total_interest_pdf, summary_total_pi_paid_pdf = amortization_totals(
                            loan_amount_pdf,
                            annual_rate,
                            int(loan_term_years),
                            pi_pdf,
                        )
                        summary_payoff_pdf = payoff_pdf

                    report = {
                        "house_purchase": [
                            ("Home Price", _fmt_currency(home_price)),
                            (
                                "Down Payment",
                                f"{down_payment_value:.2f}%" if dp_is_percent else _fmt_currency(down_payment_value),
                            ),
                            (
                                "Closing Costs",
                                f"{closing_costs_value:.2f}%" if cc_is_percent else _fmt_currency(closing_costs_value),
                            ),
                            (
                                "Earnest Money",
                                f"{earnest_money_value:.2f}%" if em_is_percent else _fmt_currency(earnest_money_value),
                            ),
                            ("Down Payment Amount", _fmt_currency(down_payment_amt_pdf)),
                            ("Loan Amount", _fmt_currency(loan_amount_pdf)),
                        ],
                        "loan_terms": [
                            ("Loan Term", f"{int(loan_term_years)} years"),
                            ("Interest Rate", f"{annual_rate:.2f}%"),
                            ("Start Date", f"{start_month_name} {int(start_year)}"),
                            ("Monthly Principal & Interest", _fmt_currency(pi_pdf)),
                            ("Estimated Payoff Date", payoff_pdf),
                        ],
                        "tax_cost": [
                            ("Include Taxes & Costs", "Yes" if include_costs else "No"),
                            (
                                "Property Tax",
                                f"{property_tax_value:.2f}%" if property_tax_is_percent else f"{_fmt_currency(property_tax_value)} / year",
                            ),
                            ("Home Insurance", f"{_fmt_currency(home_insurance_annual)} / year"),
                            ("PMI", f"{_fmt_currency(pmi_monthly)} / month"),
                            ("HOA", f"{_fmt_currency(hoa_monthly)} / month"),
                            ("Other Home Costs", f"{_fmt_currency(other_yearly)} / year"),
                            ("Tax & Cost Monthly Total", _fmt_currency(monthly_tax_pdf + monthly_ins_pdf + monthly_hoa_pdf + monthly_pmi_pdf + monthly_other_pdf)),
                        ],
                        "household": [
                            ("Include Household Expenses", "Yes" if include_household_expenses else "No"),
                            ("Daycare", f"{_fmt_currency(daycare_weekly)} / week"),
                            ("Groceries", f"{_fmt_currency(groceries_weekly)} / week"),
                            ("Utilities", f"{_fmt_currency(utilities_monthly)} / month"),
                            ("Property Expenses", f"{_fmt_currency(property_expenses_monthly)} / month"),
                            ("Household Monthly Total", _fmt_currency(household_monthly)),
                        ],
                        "vehicle": [
                            ("Include Vehicle Expenses", "Yes" if include_vehicle_expenses else "No"),
                            ("Car Tax", f"{_fmt_currency(car_tax_annual)} / year"),
                            ("Gasoline", f"{_fmt_currency(vehicle_gas_weekly)} / week"),
                            ("Car Maintenance", f"{_fmt_currency(car_maintenance_annual)} / year"),
                            ("Car Insurance", f"{_fmt_currency(car_insurance_monthly)} / month"),
                            ("Vehicle Monthly Total", _fmt_currency(vehicle_monthly)),
                        ],
                        "college": [
                            ("Include College Savings", "Yes" if include_college_savings else "No"),
                            ("529 Contribution", f"{_fmt_currency(college_529_annual)} / year per child"),
                            ("Number of Kids", str(int(num_kids))),
                            ("College Savings Monthly Total", _fmt_currency(college_monthly)),
                        ],
                        "custom_expenses": [
                            ("Include Additional Expenses", "Yes" if include_custom_expenses else "No"),
                            ("Logged Additional Expenses", str(len(custom_rows))),
                            ("Additional Expenses Monthly Total", _fmt_currency(custom_monthly_pdf)),
                        ],
                        "custom_expense_rows": custom_rows,
                        "take_home": [
                            ("Include Take Home Pay", "Yes" if include_take_home else "No"),
                            ("Logged Income Sources", str(len(take_home_rows))),
                            ("Take Home Monthly Total", _fmt_currency(take_home_monthly_pdf)),
                        ],
                        "take_home_rows": take_home_rows,
                        "extra_principal": [
                            (
                                "Scheduled Extra Payment",
                                _fmt_currency(float(st.session_state.get("scheduled_extra_amount", 0.0) or 0.0)),
                            ),
                            (
                                "Scheduled Frequency",
                                str(st.session_state.get("scheduled_extra_frequency", "Monthly")),
                            ),
                            (
                                "Scheduled Start Year",
                                str(int(st.session_state.get("scheduled_extra_start_year", 1) or 1)),
                            ),
                            (
                                "Scheduled End Year",
                                str(int(st.session_state.get("scheduled_extra_end_year", int(loan_term_years)) or int(loan_term_years))),
                            ),
                            ("Lump Sum Entries", str(len(lump_sum_rows))),
                        ],
                        "lump_sum_rows": lump_sum_rows,
                        "chart_payload": chart_payload_pdf,
                        "chart_image_path": st.session_state.get("mortgage_amortization_chart_image_path"),
                        "summary": [
                            ("House Price", _fmt_currency(home_price)),
                            ("Loan Amount", _fmt_currency(loan_amount_pdf)),
                            ("Down Payment", _fmt_currency(down_payment_amt_pdf)),
                            ("Mortgage (Monthly, P&I)", _fmt_currency(pi_pdf)),
                            ("Total of Mortgage Payments (P&I)", _fmt_currency(summary_total_pi_paid_pdf)),
                            ("Total Interest", _fmt_currency(summary_total_interest_pdf)),
                            ("Interest (Monthly, initial est.)", _fmt_currency(monthly_interest_payment_pdf)),
                            ("Total Monthly Payment", _fmt_currency(monthly_total_pdf)),
                            ("Take Home Pay (Monthly)", _fmt_currency(take_home_monthly_pdf) if include_take_home else "Not enabled"),
                            ("Affordability", "Affordable" if is_affordable_pdf else ("Exceeds take-home" if include_take_home else "N/A")),
                            ("Leftover Monthly", _fmt_currency(leftover_monthly_pdf) if leftover_monthly_pdf is not None else "N/A"),
                            ("Estimated Payoff Date", summary_payoff_pdf),
                        ],
                    }

                    st.session_state["mortgage_pdf_bytes"] = _build_mortgage_assumptions_pdf(report)
                    st.success("PDF generated. Click Download PDF to save it.")
                except Exception as exc:
                    st.error(f"Generate PDF failed: {exc}")

            pdf_bytes = st.session_state.get("mortgage_pdf_bytes")
            if pdf_bytes:
                st.download_button(
                    "Download PDF",
                    data=pdf_bytes,
                    file_name="mortgage_loan_assumptions_plan.pdf",
                    mime="application/pdf",
                    key="download_mortgage_pdf",
                )

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
            closing_costs_value=closing_costs_value,
            closing_costs_is_percent=cc_is_percent,
            earnest_money_value=earnest_money_value,
            earnest_money_is_percent=em_is_percent,
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
                monthly_interest_payment = (
                    loan_amount * ((inputs.annual_interest_rate_pct / 100.0) / 12.0)
                    if loan_amount > 0 and inputs.annual_interest_rate_pct > 0
                    else 0.0
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
                    "monthly_interest_payment": monthly_interest_payment,
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
                
                # ---- Scheduled + Lump Sum Payment Controls ----
                st.markdown("#### Extra Principal Payments")

                saved_frequency = str(st.session_state.get("scheduled_extra_frequency", "Monthly") or "Monthly")
                frequency_options = ["Monthly", "Quarterly", "Semi-Annual", "Annual"]
                if saved_frequency not in frequency_options:
                    saved_frequency = "Monthly"

                saved_start_year = int(st.session_state.get("scheduled_extra_start_year", 1) or 1)
                saved_start_year = max(1, min(saved_start_year, int(chart_data["loan_term_years"])))

                saved_end_year = int(
                    st.session_state.get("scheduled_extra_end_year", int(chart_data["loan_term_years"]))
                    or int(chart_data["loan_term_years"])
                )
                saved_end_year = max(saved_start_year, min(saved_end_year, int(chart_data["loan_term_years"])))

                recurring_cols = st.columns([1.2, 1.0, 1.0, 1.0], gap="small")
                with recurring_cols[0]:
                    recurring_extra_amount = st.number_input(
                        "Scheduled Payment ($)",
                        min_value=0.0,
                        value=float(st.session_state.get("scheduled_extra_amount", 0.0) or 0.0),
                        step=100.0,
                        key="scheduled_extra_amount",
                    )
                with recurring_cols[1]:
                    recurring_frequency_label = st.selectbox(
                        "Frequency",
                        frequency_options,
                        index=frequency_options.index(saved_frequency),
                        key="scheduled_extra_frequency",
                    )
                with recurring_cols[2]:
                    recurring_start_year = st.number_input(
                        "Start Year",
                        min_value=1,
                        max_value=int(chart_data["loan_term_years"]),
                        value=int(saved_start_year),
                        step=1,
                        key="scheduled_extra_start_year",
                    )
                with recurring_cols[3]:
                    recurring_end_year = st.number_input(
                        "End Year",
                        min_value=int(recurring_start_year),
                        max_value=int(chart_data["loan_term_years"]),
                        value=max(int(recurring_start_year), int(saved_end_year)),
                        step=1,
                        key="scheduled_extra_end_year",
                    )

                frequency_to_months = {
                    "Monthly": 1,
                    "Quarterly": 3,
                    "Semi-Annual": 6,
                    "Annual": 12,
                }
                recurring_frequency_months = frequency_to_months.get(recurring_frequency_label, 1)
                recurring_start_month = ((int(recurring_start_year) - 1) * 12) + 1
                recurring_end_month = int(recurring_end_year) * 12

                if "lump_sum_payments" not in st.session_state:
                    st.session_state["lump_sum_payments"] = []

                lump_cols = st.columns([1.0, 1.2, 0.8], gap="small")
                with lump_cols[0]:
                    st.number_input(
                        "Lump Sum Year",
                        min_value=1,
                        max_value=int(chart_data["loan_term_years"]),
                        step=1,
                        key="lump_sum_year_input",
                    )
                with lump_cols[1]:
                    st.number_input(
                        "Lump Sum Amount ($)",
                        min_value=0.0,
                        step=100.0,
                        key="lump_sum_amount_input",
                    )

                def _add_lump_sum() -> None:
                    year = int(st.session_state.get("lump_sum_year_input", 1))
                    amount = float(st.session_state.get("lump_sum_amount_input", 0.0))
                    if amount <= 0:
                        st.session_state["lump_sum_error"] = "Lump sum amount must be greater than $0."
                        return
                    st.session_state.setdefault("lump_sum_payments", []).append({
                        "Year": year,
                        "Amount": amount,
                    })
                    st.session_state["lump_sum_amount_input"] = 0.0
                    st.session_state.pop("lump_sum_error", None)

                with lump_cols[2]:
                    st.button("Add Lump Sum", key="add_lump_sum_btn", on_click=_add_lump_sum, width="stretch")

                if st.session_state.get("lump_sum_error"):
                    st.warning(st.session_state["lump_sum_error"])

                lump_log_container = st.container(height=130)
                with lump_log_container:
                    lump_rows = st.session_state.get("lump_sum_payments", [])
                    if not lump_rows:
                        st.caption("No lump sum payments added.")
                    else:
                        for idx, row in enumerate(lump_rows):
                            row_cols = st.columns([0.9, 1.1, 0.3], gap="small")
                            with row_cols[0]:
                                st.number_input(
                                    "Year",
                                    min_value=1,
                                    max_value=int(chart_data["loan_term_years"]),
                                    step=1,
                                    value=int(row.get("Year", 1)),
                                    key=f"lump_year_{idx}",
                                    label_visibility="collapsed",
                                )
                            with row_cols[1]:
                                st.number_input(
                                    "Amount",
                                    min_value=0.0,
                                    step=100.0,
                                    value=float(row.get("Amount", 0.0)),
                                    key=f"lump_amount_{idx}",
                                    label_visibility="collapsed",
                                )
                            with row_cols[2]:
                                if st.button("🗑️", key=f"lump_delete_{idx}"):
                                    st.session_state["lump_sum_payments"] = [
                                        r for i, r in enumerate(st.session_state.get("lump_sum_payments", [])) if i != idx
                                    ]
                                    st.rerun()

                        # persist edits
                        updated_lumps = []
                        for idx, _ in enumerate(st.session_state.get("lump_sum_payments", [])):
                            updated_lumps.append(
                                {
                                    "Year": int(st.session_state.get(f"lump_year_{idx}", 1)),
                                    "Amount": float(st.session_state.get(f"lump_amount_{idx}", 0.0)),
                                }
                            )
                        st.session_state["lump_sum_payments"] = updated_lumps

                lump_sum_by_month = {}
                for row in st.session_state.get("lump_sum_payments", []):
                    year_num = max(1, min(int(chart_data["loan_term_years"]), int(row.get("Year", 1))))
                    amount = max(0.0, float(row.get("Amount", 0.0)))
                    month_index = (year_num - 1) * 12 + 1
                    lump_sum_by_month[month_index] = lump_sum_by_month.get(month_index, 0.0) + amount

                # Persist amortization adjustment state + cache image for profile/PDF reuse
                chart_payload_current = {
                    "loan_amount": float(chart_data["loan_amount"]),
                    "annual_rate": float(chart_data["annual_rate"]),
                    "loan_term_years": int(chart_data["loan_term_years"]),
                    "pi": float(chart_data["pi"]),
                    "recurring_extra_amount": float(recurring_extra_amount),
                    "recurring_frequency_months": int(recurring_frequency_months),
                    "recurring_start_month": int(recurring_start_month),
                    "recurring_end_month": int(recurring_end_month),
                    "lump_sum_rows": list(st.session_state.get("lump_sum_payments", [])),
                    "start_year": int(inputs.start_year),
                    "start_month": int(inputs.start_month),
                }
                st.session_state["mortgage_chart_payload"] = chart_payload_current

                cached_chart_path = _ensure_amortization_chart_cached(chart_payload_current)
                if cached_chart_path:
                    st.session_state["mortgage_amortization_chart_image_path"] = cached_chart_path

                # ---- Select amortization for chart ----
                has_recurring = recurring_extra_amount > 0
                has_lumps = any(float(row.get("Amount", 0.0)) > 0 for row in st.session_state.get("lump_sum_payments", []))

                if has_recurring or has_lumps:
                    dynamic_total_interest, dynamic_total_pi_paid, dynamic_months_to_payoff = amortization_totals_with_adjustments(
                        chart_data["loan_amount"],
                        chart_data["annual_rate"],
                        chart_data["loan_term_years"],
                        chart_data["pi"],
                        recurring_extra_amount=recurring_extra_amount,
                        recurring_frequency_months=recurring_frequency_months,
                        recurring_start_month=recurring_start_month,
                        recurring_end_month=recurring_end_month,
                        lump_sum_by_month=lump_sum_by_month,
                    )
                    dynamic_payoff = payoff_date_from_months(
                        inputs.start_year,
                        inputs.start_month,
                        dynamic_months_to_payoff,
                    )

                    amortization_df = amortization_schedule_with_adjustments(
                        chart_data["loan_amount"],
                        chart_data["annual_rate"],
                        chart_data["loan_term_years"],
                        chart_data["pi"],
                        recurring_extra_amount=recurring_extra_amount,
                        recurring_frequency_months=recurring_frequency_months,
                        recurring_start_month=recurring_start_month,
                        recurring_end_month=recurring_end_month,
                        lump_sum_by_month=lump_sum_by_month,
                    )
                else:
                    dynamic_total_interest = total_interest
                    dynamic_total_pi_paid = total_pi_paid
                    dynamic_months_to_payoff = int(chart_data["loan_term_years"]) * 12
                    dynamic_payoff = payoff

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

                chart_summary_text = (
                    f"Total of Mortgage Payments (P&I): ${dynamic_total_pi_paid:,.2f}\n"
                    f"Total Interest: ${dynamic_total_interest:,.2f}\n"
                    f"Mortgage Payoff Date: {dynamic_payoff}"
                )
                ax.text(
                    0.98,
                    0.98,
                    chart_summary_text,
                    transform=ax.transAxes,
                    fontsize=9.5,
                    va="top",
                    ha="right",
                    bbox={
                        "boxstyle": "round,pad=0.4",
                        "facecolor": "white",
                        "edgecolor": "#9e9e9e",
                        "alpha": 0.9,
                    },
                )

                st.pyplot(fig)

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
                summary_display_total_pi_paid = dynamic_total_pi_paid
                summary_display_total_interest = dynamic_total_interest
                summary_display_payoff = dynamic_payoff
                
                st.markdown("### Summary")

                c1, c2 = st.columns(2)
                with c1:
                    st.metric("House Price", f"${summary['home_price']:,.2f}")
                    st.metric("Loan Amount", f"${summary['loan_amount']:,.2f}")
                    st.metric("Down Payment", f"${summary['down_payment_amt']:,.2f}")
                    st.metric("Total of Mortgage Payments (P&I)", f"${summary_display_total_pi_paid:,.2f}")
                    st.metric("Total Interest", f"${summary_display_total_interest:,.2f}")
                    st.metric("Mortgage Payoff Date", summary_display_payoff)
                with c2:
                    st.metric(
                        "Mortgage (Monthly)",
                        f"${summary['pi']:,.0f}",
                        help="Principal & Interest only"
                    )
                    st.metric(
                        "Interest (Monthly)",
                        f"${summary['monthly_interest_payment']:,.0f}",
                        help="Estimated first-month interest portion of the mortgage payment"
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
