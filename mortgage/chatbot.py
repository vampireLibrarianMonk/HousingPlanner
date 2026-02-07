"""Mortgage chatbot helpers for cash-to-close and affordability guidance."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Dict, Any, List, Tuple

import streamlit as st

from config.pricing import (
    CLAUDE_INFERENCE_PROFILES,
    PRICING_REGISTRY,
    PRICING_VERSION,
    estimate_request_cost,
)
from .calculations import monthly_pi_payment
from .costs import compute_costs_monthly
from .models import MortgageInputs


@dataclass
class MortgageChatContext:
    state: str | None = None
    county: str | None = None
    city: str | None = None
    closing_month: str | None = None
    closing_year: int | None = None
    hoa: str | None = None
    take_home_monthly: float | None = None
    obligations_monthly: float | None = None
    liquid_assets: float | None = None


def _ensure_mortgage_chat_state() -> None:
    if "mortgage_chat_history" not in st.session_state:
        st.session_state["mortgage_chat_history"] = []
    if "mortgage_cost_records" not in st.session_state:
        st.session_state["mortgage_cost_records"] = []
    if "mortgage_cost_breakdown" not in st.session_state:
        st.session_state["mortgage_cost_breakdown"] = []
    if "mortgage_inference_profile" not in st.session_state:
        st.session_state["mortgage_inference_profile"] = None
    if "mortgage_profile_initialized" not in st.session_state:
        st.session_state["mortgage_profile_initialized"] = False
    if "mortgage_chat_context" not in st.session_state:
        st.session_state["mortgage_chat_context"] = MortgageChatContext().__dict__


def _get_pricing_key_for_profile(profile_id: str) -> str | None:
    if not profile_id:
        return None
    if "sonnet-4-5" in profile_id:
        return "anthropic.claude-sonnet-4-5"
    if "opus-4-5" in profile_id:
        return "anthropic.claude-opus-4-5"
    if "opus-4-1" in profile_id:
        return "anthropic.claude-opus-4-1"
    if "sonnet-4" in profile_id:
        return "anthropic.claude-sonnet-4"
    if "claude-3-haiku" in profile_id:
        return "anthropic.claude-3-haiku"
    if "claude-3-sonnet" in profile_id:
        return "anthropic.claude-3-sonnet"
    if "claude-3-opus" in profile_id:
        return "anthropic.claude-3-opus"
    if "haiku" in profile_id:
        return "anthropic.claude-haiku-4-5"
    return None


def _record_cost(input_tokens: int, output_tokens: int) -> None:
    profile_id = st.session_state.get("mortgage_inference_profile")
    model_key = _get_pricing_key_for_profile(profile_id or "")
    if not model_key or model_key not in PRICING_REGISTRY:
        return
    estimated_cost = estimate_request_cost(model_key, input_tokens, output_tokens)
    st.session_state["mortgage_cost_records"].append(
        {
            "request_id": profile_id or "unknown",
            "model_id": model_key,
            "inference_profile": profile_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "pricing_version": PRICING_VERSION,
        }
    )


def _format_currency(value: float) -> str:
    return rf"\${value:,.0f}"


def _format_currency_range(low: float, high: float) -> str:
    if abs(low - high) < 1e-6:
        return _format_currency(low)
    return rf"\${low:,.0f} – \${high:,.0f}"


def _range_from_percent(amount: float, low_pct: float, high_pct: float) -> Tuple[float, float]:
    return amount * low_pct, amount * high_pct


def _parse_amount(text: str) -> float | None:
    match = re.search(r"\$?([0-9,.]+)", text)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def _update_context_from_text(text: str, context: Dict[str, Any]) -> None:
    lowered = text.lower()
    state_match = re.search(r"\bstate[:\s]+([a-z]{2})\b", lowered)
    if state_match:
        context["state"] = state_match.group(1).upper()

    city_match = re.search(r"\bcity[:\s]+([a-z\s]+)", lowered)
    if city_match:
        context["city"] = city_match.group(1).strip().title()

    county_match = re.search(r"\bcounty[:\s]+([a-z\s]+)", lowered)
    if county_match:
        context["county"] = county_match.group(1).strip().title()

    closing_match = re.search(
        r"\b(clos(?:ing|e)\s*(?:month|date)?)[\s:]*([a-z]{3,9})\s*(\d{4})",
        lowered,
    )
    if closing_match:
        context["closing_month"] = closing_match.group(2).title()
        context["closing_year"] = int(closing_match.group(3))

    hoa_match = re.search(r"\bhoa\b.*\b(yes|no|unknown)\b", lowered)
    if hoa_match:
        context["hoa"] = hoa_match.group(1)

    if "income" in lowered or "take home" in lowered:
        amount = _parse_amount(lowered)
        if amount is not None:
            if "year" in lowered or "annual" in lowered:
                context["take_home_monthly"] = amount / 12.0
            else:
                context["take_home_monthly"] = amount

    if "obligation" in lowered or "debt" in lowered:
        amount = _parse_amount(lowered)
        if amount is not None:
            context["obligations_monthly"] = amount

    if "asset" in lowered or "liquid" in lowered or "cash" in lowered:
        amount = _parse_amount(lowered)
        if amount is not None:
            context["liquid_assets"] = amount


def _build_cash_to_close_breakdown(
    inputs: MortgageInputs,
    down_payment_amt: float,
    loan_amount: float,
    pi_monthly: float,
) -> Tuple[str, List[Dict[str, str]]]:
    closing_low, closing_high = _range_from_percent(inputs.home_price, 0.02, 0.04)
    earnest_low, earnest_high = _range_from_percent(inputs.home_price, 0.01, 0.03)
    diligence_low, diligence_high = 700.0, 2000.0
    repair_low, repair_high = _range_from_percent(inputs.home_price, 0.01, 0.02)

    costs_monthly = compute_costs_monthly(inputs)
    property_tax_monthly = costs_monthly["property_tax_monthly"]
    insurance_monthly = costs_monthly["home_insurance_monthly"]

    escrow_low = property_tax_monthly * 2
    escrow_high = property_tax_monthly * 6
    prepaid_interest = pi_monthly * 0.5
    prepaids_low = insurance_monthly * 12 + escrow_low + prepaid_interest
    prepaids_high = insurance_monthly * 12 + escrow_high + prepaid_interest

    cash_to_close_low = down_payment_amt + closing_low + prepaids_low - earnest_low
    cash_to_close_high = down_payment_amt + closing_high + prepaids_high - earnest_high

    total_monthly = (
        pi_monthly
        + costs_monthly["property_tax_monthly"]
        + costs_monthly["home_insurance_monthly"]
        + costs_monthly["hoa_monthly"]
        + costs_monthly["pmi_monthly"]
        + costs_monthly["other_home_monthly"]
    )
    emergency_low = total_monthly * 3
    emergency_high = total_monthly * 6

    safe_target_low = cash_to_close_low + diligence_low + repair_low + emergency_low
    safe_target_high = cash_to_close_high + diligence_high + repair_high + emergency_high

    breakdown = [
        {"Entry": "Down payment", "Cost": _format_currency(down_payment_amt)},
        {"Entry": "Closing costs (2–4%)", "Cost": _format_currency_range(closing_low, closing_high)},
        {"Entry": "Prepaids + escrow", "Cost": _format_currency_range(prepaids_low, prepaids_high)},
        {"Entry": "Earnest money (credited)", "Cost": _format_currency_range(earnest_low, earnest_high)},
        {"Entry": "Buyer diligence", "Cost": _format_currency_range(diligence_low, diligence_high)},
        {"Entry": "Move-in / repair buffer", "Cost": _format_currency_range(repair_low, repair_high)},
        {"Entry": "Emergency fund (3–6 mo)", "Cost": _format_currency_range(emergency_low, emergency_high)},
        {"Entry": "Cash to close (after earnest)", "Cost": _format_currency_range(cash_to_close_low, cash_to_close_high)},
        {"Entry": "Recommended safe cash target", "Cost": _format_currency_range(safe_target_low, safe_target_high)},
    ]

    summary = (
        "**Estimated cash to close**\n"
        f"- {_format_currency_range(cash_to_close_low, cash_to_close_high)}\n\n"
        "**Recommended safe cash target**\n"
        f"- {_format_currency_range(safe_target_low, safe_target_high)}\n\n"
        "_Earnest money is credited toward your down payment, so you need it early even though it reduces cash to close._"
    )
    return summary, breakdown


def _build_monthly_breakdown(inputs: MortgageInputs, loan_amount: float) -> Tuple[str, List[str]]:
    pi_monthly = monthly_pi_payment(
        loan_amount,
        inputs.annual_interest_rate_pct,
        inputs.loan_term_years,
    )
    costs = compute_costs_monthly(inputs)
    monthly_total = pi_monthly + sum(costs.values())

    lines = [
        f"Mortgage P&I: {_format_currency(pi_monthly)}",
        f"Property tax: {_format_currency(costs['property_tax_monthly'])}/mo",
        f"Homeowners insurance: {_format_currency(costs['home_insurance_monthly'])}/mo",
        f"HOA: {_format_currency(costs['hoa_monthly'])}/mo",
        f"PMI: {_format_currency(costs['pmi_monthly'])}/mo",
        f"Other home costs: {_format_currency(costs['other_home_monthly'])}/mo",
    ]
    summary = (
        f"**Estimated recurring housing costs:** {_format_currency(monthly_total)}/mo\n"
        "(Includes P&I, taxes, insurance, HOA, PMI, and other home costs.)"
    )
    return summary, lines


def _build_affordability_section(
    monthly_total: float,
    context: Dict[str, Any],
) -> str:
    take_home = context.get("take_home_monthly")
    if not take_home:
        return "Provide your monthly take-home income for an affordability check."
    ratio = monthly_total / take_home if take_home > 0 else 0
    threshold_flag = "⚠️" if ratio > 0.35 else "✅"
    return (
        f"{threshold_flag} Housing cost is ~{ratio:.0%} of take-home pay. "
        "(35%+ is typically considered stretched.)"
    )


def _build_response(
    inputs: MortgageInputs,
    down_payment_amt: float,
    loan_amount: float,
    context: Dict[str, Any],
) -> Tuple[str, List[Dict[str, str]]]:
    dp_pct = (down_payment_amt / inputs.home_price * 100.0) if inputs.home_price else 0.0
    pmi_avoid = dp_pct >= 20.0

    pi_monthly = monthly_pi_payment(
        loan_amount,
        inputs.annual_interest_rate_pct,
        inputs.loan_term_years,
    )

    cash_summary, breakdown = _build_cash_to_close_breakdown(
        inputs,
        down_payment_amt,
        loan_amount,
        pi_monthly,
    )
    monthly_summary, monthly_lines = _build_monthly_breakdown(inputs, loan_amount)
    monthly_total = pi_monthly + sum(compute_costs_monthly(inputs).values())

    missing = []
    if not context.get("state"):
        missing.append("state")
    if not context.get("closing_month"):
        missing.append("closing month/year")
    if context.get("hoa") is None:
        missing.append("HOA (yes/no)")

    missing_text = ""
    if missing:
        missing_text = (
            "\n\n**Missing inputs:** "
            + ", ".join(missing)
            + " (share in chat to refine taxes/escrows)."
        )

    response = (
        "### Down Payment Check\n"
        f"- Down payment: {_format_currency(down_payment_amt)} ({dp_pct:.1f}% of price)\n"
        f"- PMI avoidance: {'Yes (≥20%)' if pmi_avoid else 'No (PMI likely)'}\n"
        "- Down payment is **separate** from closing/prepaid costs.\n\n"
        "### Cash-to-Close & Reserves\n"
        f"{cash_summary}\n\n"
        "### Monthly Housing Estimate\n"
        f"{monthly_summary}\n"
        + "\n".join([f"- {line}" for line in monthly_lines])
        + "\n\n"
        "### Affordability Check\n"
        f"{_build_affordability_section(monthly_total, context)}\n"
        + missing_text
    )
    return response, breakdown


def render_mortgage_chatbot(
    inputs: MortgageInputs,
    down_payment_amt: float,
    loan_amount: float,
    include_take_home: bool,
    take_home_monthly: float | None,
) -> None:
    _ensure_mortgage_chat_state()

    context = st.session_state.get("mortgage_chat_context", {})
    if include_take_home and take_home_monthly:
        context["take_home_monthly"] = take_home_monthly
    st.session_state["mortgage_chat_context"] = context

    with st.expander("Mortgage Chatbot", expanded=False):
        st.caption(
            "Ask about cash-to-close, monthly costs, down payment validation, and affordability."
        )

        with st.expander("Inference Profile", expanded=False):
            profile_rows = []
            for profile in CLAUDE_INFERENCE_PROFILES:
                pricing_key = _get_pricing_key_for_profile(profile.profile_id)
                if pricing_key and pricing_key in PRICING_REGISTRY:
                    pricing = PRICING_REGISTRY[pricing_key]
                    label = (
                        f"{profile.name} "
                        f"(${pricing.input_per_1m:.2f}/1M in, ${pricing.output_per_1m:.2f}/1M out)"
                    )
                    sort_cost = pricing.input_per_1m + pricing.output_per_1m
                else:
                    label = f"{profile.name} (pricing TBD)"
                    sort_cost = float("inf")
                profile_rows.append((sort_cost, profile.profile_id, label))

            profile_rows.sort(key=lambda row: row[0])
            profile_options = {profile_id: label for _, profile_id, label in profile_rows}
            default_profile_id = st.session_state.get("mortgage_inference_profile")
            if not st.session_state.get("mortgage_profile_initialized"):
                default_profile_id = next(iter(profile_options.keys()))
                st.session_state["mortgage_inference_profile"] = default_profile_id
                st.session_state["mortgage_profile_initialized"] = True
            elif default_profile_id not in profile_options:
                default_profile_id = next(iter(profile_options.keys()))

            selected_profile = st.selectbox(
                "Select Model",
                options=list(profile_options.keys()),
                format_func=lambda pid: profile_options[pid],
                index=list(profile_options.keys()).index(default_profile_id),
                key="mortgage_inference_selector",
            )
            st.session_state["mortgage_inference_profile"] = selected_profile

        total_input = sum(
            record.get("input_tokens", 0)
            for record in st.session_state.get("mortgage_cost_records", [])
        )
        total_output = sum(
            record.get("output_tokens", 0)
            for record in st.session_state.get("mortgage_cost_records", [])
        )
        total_cost = sum(
            record.get("estimated_cost_usd", 0.0)
            for record in st.session_state.get("mortgage_cost_records", [])
        )
        st.caption(f"Usage: {total_input} in / {total_output} out · ${total_cost:.6f}")

        chat_container = st.container(height=260)
        with chat_container:
            if not st.session_state["mortgage_chat_history"]:
                st.info("Ask: 'What cash do I need to close?' or 'Check my down payment'.")
            for msg in st.session_state["mortgage_chat_history"]:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])

        if st.session_state.get("mortgage_cost_breakdown"):
            st.markdown("#### Cost Breakdown")
            breakdown_rows = st.session_state["mortgage_cost_breakdown"]
            for row in breakdown_rows:
                entry = row.get("Entry", "")
                cost = row.get("Cost", "")
                st.markdown(
                    f"- **{entry}**: {cost}"
                )

        user_input = st.chat_input(
            "Ask about cash-to-close, monthly costs, or affordability...",
            key="mortgage_chat_input",
        )
        if user_input:
            _update_context_from_text(user_input, context)
            response_text, breakdown = _build_response(
                inputs,
                down_payment_amt,
                loan_amount,
                context,
            )
            st.session_state["mortgage_chat_history"].append(
                {"role": "user", "content": user_input}
            )
            st.session_state["mortgage_chat_history"].append(
                {"role": "assistant", "content": response_text}
            )
            st.session_state["mortgage_cost_breakdown"] = breakdown

            _record_cost(max(1, len(user_input.split())), max(20, len(response_text.split())))
            st.rerun()