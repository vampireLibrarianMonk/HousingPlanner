"""Mortgage chatbot helpers for cash-to-close and affordability guidance."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Dict, Any, List, Tuple

import boto3
import streamlit as st

from config.pricing import (
    CLAUDE_INFERENCE_PROFILES,
    estimate_request_cost,
    get_llm_pricing_registry,
    get_pricing_version,
)
from profile.costs import _recalculate_costs
from profile.state_io import auto_save_profile
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
    registry = get_llm_pricing_registry()
    if not model_key or model_key not in registry:
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
            "pricing_version": get_pricing_version(),
        }
    )
    # Auto-save profile to persist costs
    auto_save_profile()


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


def _parse_all_amounts(text: str) -> List[float]:
    """Extract all dollar amounts from text."""
    matches = re.findall(r"\$?([0-9,.]+)\s*[kK]?", text)
    amounts = []
    for match in matches:
        try:
            val = float(match.replace(",", ""))
            # Check if followed by K/k for thousands
            idx = text.find(match)
            if idx >= 0 and idx + len(match) < len(text):
                next_char = text[idx + len(match):idx + len(match) + 1].lower()
                if next_char == 'k':
                    val *= 1000
            amounts.append(val)
        except ValueError:
            continue
    return amounts


def _try_extract_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}


def _normalize_history_markdown(text: str) -> str:
    """Normalize markdown so chat history renders in a single font size."""
    if not text:
        return text
    normalized = re.sub(r"^\s*#{1,6}\s*(.+)$", r"**\g<1>**", text, flags=re.MULTILINE)
    def _format_currency_range_match(match: re.Match) -> str:
        low = match.group("low")
        high = match.group("high")
        return f"${low} – ${high}"

    normalized = re.sub(
        r"(?<!\d)\$?(?P<low>\d{1,3}(?:,\d{3})+(?:\.\d{2})?)\s*[-–—]\s*\$?(?P<high>\d{1,3}(?:,\d{3})+(?:\.\d{2})?)",
        _format_currency_range_match,
        normalized,
    )
    normalized = re.sub(
        r"(?<![$\d])\b(\d{1,3}(?:,\d{3})+(?:\.\d{2})?)\b(?!\s*(?:[%A-Za-z]|/|\w))",
        r"$\1",
        normalized,
    )
    return normalized


def _invoke_mortgage_llm(prompt: str, *, model_id: str) -> Dict[str, Any]:
    client = boto3.client("bedrock-runtime")
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1400,
        "temperature": 0.2,
        "messages": [
            {
                "role": "user",
                "content": prompt,
            }
        ],
    }
    response = client.invoke_model(modelId=model_id, body=json.dumps(body))
    payload = json.loads(response["body"].read())
    content = payload.get("content", [])
    text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
    usage = payload.get("usage") or {}
    return {
        "raw": text,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
    }


def _build_mortgage_llm_prompt(
    *,
    user_input: str,
    inputs: MortgageInputs,
    down_payment_amt: float,
    loan_amount: float,
    context: Dict[str, Any],
) -> str:
    return (
        "You are a mortgage cash-to-close and affordability assistant. "
        "Use the provided inputs to calculate cash-to-close, monthly costs, affordability, and any remaining budget "
        "after amounts the user says they already paid.\n\n"
        "You MUST compute numbers yourself (do not say you cannot compute). Use the formulas and inputs below.\n\n"
        "Formulas:\n"
        "- Monthly P&I payment for a fixed-rate mortgage:\n"
        "  r = annual_rate_pct / 100 / 12\n"
        "  n = loan_term_years * 12\n"
        "  payment = loan_amount * r * (1+r)^n / ((1+r)^n - 1)  (if r>0)\n"
        "- Property tax monthly = (home_price * property_tax_pct / 100) / 12  OR  property_tax_value / 12\n"
        "- Home insurance monthly = home_insurance_annual / 12\n"
        "- Other home costs monthly = other_yearly / 12\n"
        "- Closing costs range = 2%–4% of home_price\n"
        "- Earnest money range = 1%–3% of home_price\n"
        "- Buyer diligence range = $700–$2,000\n"
        "- Move-in/repair buffer = 1%–2% of home_price\n"
        "- Prepaids/escrow = 12 months insurance + 2–6 months property tax + 0.5 * monthly P&I\n"
        "- Cash to close (after earnest) = down_payment + closing_costs + prepaids - earnest_money\n"
        "- Monthly total housing = P&I + property tax + insurance + HOA + PMI + other home costs\n"
        "- Emergency fund = 3–6 months of total housing\n"
        "- Recommended safe cash target = cash_to_close + diligence + move-in buffer + emergency fund\n\n"
        "Your response must include:\n"
        "1) A markdown response for the chat (use headings and bullets).\n"
        "2) A Cost Breakdown list with entries and values.\n"
        "3) Parsed fields (amounts mentioned as already paid, question type, and any inputs the user provided).\n\n"
        "Important: The response and breakdown must ONLY include information relevant to the question_type.\n"
        "Use these rules:\n"
        "- cash_to_close: include down payment, closing costs, prepaids/escrow, earnest money, cash-to-close, safe target.\n"
        "- leftover_budget: include what has been paid and remaining cash-to-close and remaining closing costs only.\n"
        "- monthly: include monthly P&I, taxes, insurance, HOA, PMI, other, total monthly only.\n"
        "- affordability: include monthly total + take-home comparison only.\n"
        "- down_payment: include down payment amount and PMI avoidance only.\n"
        "If itemized closing costs are provided, use those numbers (buyer-side only) and show each line item when\n"
        "answering cash_to_close or leftover_budget questions.\n"
        "If explicit paid amounts are provided, treat them as authoritative over any inference from the user text.\n"
        "If the user states a percent paid for closing costs and there is no explicit paid amount, compute it from home_price.\n"
        "Do NOT include sections outside the relevant category.\n\n"
        "Return ONLY valid JSON with this exact shape (no extra text):\n"
        "{\n"
        "  \"response_markdown\": \"...\",\n"
        "  \"breakdown\": [\n"
        "    {\"Entry\": \"...\", \"Cost\": \"...\"}\n"
        "  ],\n"
        "  \"parsed\": {\n"
        "    \"question_type\": \"cash_to_close|leftover_budget|monthly|affordability|down_payment|other\",\n"
        "    \"response_sections\": [\"cash_to_close\", \"monthly\", \"affordability\", \"leftover_budget\", \"down_payment\"],\n"
        "    \"breakdown_categories\": [\"cash_to_close\", \"monthly\", \"affordability\", \"leftover_budget\", \"down_payment\"],\n"
        "    \"down_payment_paid\": number|null,\n"
        "    \"closing_costs_paid\": number|null,\n"
        "    \"earnest_money_paid\": number|null,\n"
        "    \"state\": string|null,\n"
        "    \"closing_month\": string|null,\n"
        "    \"closing_year\": number|null,\n"
        "    \"hoa\": \"yes|no|unknown\"|null,\n"
        "    \"take_home_monthly\": number|null\n"
        "  }\n"
        "}\n\n"
        "Context for this request:\n"
        f"User question: {user_input}\n\n"
        "Mortgage inputs:\n"
        f"- home_price: {inputs.home_price}\n"
        f"- down_payment_amt: {down_payment_amt}\n"
        f"- down_payment_is_percent: {inputs.down_payment_is_percent}\n"
        f"- down_payment_value: {inputs.down_payment_value}\n"
        f"- closing_costs_value: {inputs.closing_costs_value}\n"
        f"- closing_costs_is_percent: {inputs.closing_costs_is_percent}\n"
        f"- earnest_money_value: {inputs.earnest_money_value}\n"
        f"- earnest_money_is_percent: {inputs.earnest_money_is_percent}\n"
        f"- loan_amount: {loan_amount}\n"
        f"- annual_interest_rate_pct: {inputs.annual_interest_rate_pct}\n"
        f"- loan_term_years: {inputs.loan_term_years}\n"
        f"- property_tax_value: {inputs.property_tax_value}\n"
        f"- property_tax_is_percent: {inputs.property_tax_is_percent}\n"
        f"- home_insurance_annual: {inputs.home_insurance_annual}\n"
        f"- pmi_monthly: {inputs.pmi_monthly}\n"
        f"- hoa_monthly: {inputs.hoa_monthly}\n"
        f"- other_yearly: {inputs.other_yearly}\n\n"
        "Existing context (may be null):\n"
        f"- state: {context.get('state')}\n"
        f"- county: {context.get('county')}\n"
        f"- city: {context.get('city')}\n"
        f"- closing_month: {context.get('closing_month')}\n"
        f"- closing_year: {context.get('closing_year')}\n"
        f"- hoa: {context.get('hoa')}\n"
        f"- take_home_monthly: {context.get('take_home_monthly')}\n"
        "Paid amounts (if provided by the user in chat):\n"
        f"- down_payment_paid: {context.get('paid_amounts', {}).get('down_payment_paid')}\n"
        f"- closing_costs_paid: {context.get('paid_amounts', {}).get('closing_costs_paid')}\n"
        f"- earnest_money_paid: {context.get('paid_amounts', {}).get('earnest_money_paid')}\n"
        "Buyer-side closing costs detail (itemized):\n"
        f"- realtor_fee_value: {context.get('closing_costs_itemized', {}).get('realtor_fee_value')}\n"
        f"- realtor_fee_unit: {context.get('closing_costs_itemized', {}).get('realtor_fee_unit')}\n"
        f"- realtor_fee_amount: {context.get('closing_costs_itemized', {}).get('realtor_fee_amount')}\n"
        f"- title_escrow_fees: {context.get('closing_costs_itemized', {}).get('title_escrow_fees')}\n"
        f"- loan_origination_fees: {context.get('closing_costs_itemized', {}).get('loan_origination_fees')}\n"
        f"- recording_transfer_taxes: {context.get('closing_costs_itemized', {}).get('recording_transfer_taxes')}\n"
        f"- inspection_items: {context.get('closing_costs_itemized', {}).get('inspection_items')}\n"
        f"- inspection_total: {context.get('closing_costs_itemized', {}).get('inspection_total')}\n"
        f"- itemized_total: {context.get('closing_costs_itemized', {}).get('itemized_total')}\n"
    )


def _update_context_from_llm(context: Dict[str, Any], parsed: Dict[str, Any]) -> None:
    for key in [
        "state",
        "county",
        "city",
        "closing_month",
        "closing_year",
        "hoa",
        "take_home_monthly",
    ]:
        if parsed.get(key) is not None:
            context[key] = parsed.get(key)


def _filter_breakdown_by_question_type(
    breakdown: List[Dict[str, Any]],
    question_type: str,
) -> List[Dict[str, Any]]:
    if not breakdown or not question_type:
        return breakdown

    allowed_keywords = {
        "cash_to_close": [
            "down payment",
            "closing costs",
            "prepaids",
            "escrow",
            "earnest",
            "cash to close",
            "safe cash",
            "safe target",
            "recommended",
        ],
        "leftover_budget": [
            "already paid",
            "paid",
            "remaining",
            "closing costs",
            "cash to close",
            "down payment",
            "prepaids",
            "escrow",
        ],
        "monthly": [
            "p&i",
            "principal",
            "interest",
            "property tax",
            "insurance",
            "hoa",
            "pmi",
            "other home",
            "total monthly",
            "monthly",
        ],
        "affordability": [
            "afford",
            "take home",
            "leftover",
            "ratio",
            "monthly total",
            "budget",
        ],
        "down_payment": [
            "down payment",
            "pmi",
            "avoid",
        ],
    }

    keywords = allowed_keywords.get(question_type, [])
    if not keywords:
        return breakdown

    filtered: List[Dict[str, Any]] = []
    for row in breakdown:
        entry = str(row.get("Entry", "")).lower()
        cost = str(row.get("Cost", "")).lower()
        if any(keyword in entry or keyword in cost for keyword in keywords):
            filtered.append(row)
    return filtered or breakdown


def _detect_question_type(text: str) -> str:
    """Detect the type of question being asked."""
    lowered = text.lower()
    
    # Check for leftover/remaining/budget questions
    leftover_patterns = [
        r"leftover",
        r"remaining",
        r"left\s*over",
        r"what\s+left",
        r"what['']?s\s+left",
        r"how\s+much.*(?:left|remain)",
        r"budget.*(?:left|remain)",
        r"(?:already|paid|spent).*(?:how\s+much|what)",
        r"what.*(?:left|remain)",
        r"(?:left|remain).*(?:budget|in\s+the)",
    ]
    for pattern in leftover_patterns:
        if re.search(pattern, lowered):
            return "leftover_calculation"
    
    # Check for "if I make/pay" questions with amounts
    if re.search(r"(?:if\s+i|when\s+i|once\s+i)\s+(?:make|pay|paid|cover)", lowered):
        if re.search(r"(?:down\s*payment|closing|earnest)", lowered):
            return "leftover_calculation"
    
    # Check for "already paid" or "if I paid" questions
    if re.search(r"(?:already|if\s+i|i\s+(?:have\s+)?paid|spent)", lowered):
        if re.search(r"(?:down\s*payment|closing|earnest)", lowered):
            return "leftover_calculation"
    
    # Check for questions mentioning both payment and budget/left
    if re.search(r"(?:pay|paid|make).*(?:budget|left)", lowered):
        return "leftover_calculation"
    
    # Check for cash-to-close questions
    if re.search(r"cash.*(close|need)|need.*cash|what.*need.*close", lowered):
        return "cash_to_close"
    
    # Check for down payment questions
    if re.search(r"down\s*payment|check.*(?:down|payment)", lowered):
        return "down_payment"
    
    # Check for monthly payment questions
    if re.search(r"month|payment|p&i|piti", lowered):
        return "monthly"
    
    # Check for affordability questions
    if re.search(r"afford|can\s+i|budget", lowered):
        return "affordability"
    
    return "general"


def _safe_parse_amount(
    raw_value: str | None,
    source_text: str,
    match_end: int | None = None,
) -> float | None:
    if not raw_value:
        return None
    cleaned = raw_value.replace(",", "").strip()
    if not cleaned or not re.search(r"\d", cleaned):
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if match_end is not None and match_end < len(source_text):
        suffix = source_text[match_end:match_end + 1].lower()
        if suffix == "k":
            value *= 1000
    return value


def _extract_paid_amounts(text: str, down_payment_amt: float = 0.0) -> Dict[str, float]:
    """Extract specific amounts that user mentions they've already paid."""
    lowered = text.lower()
    result: Dict[str, float] = {}
    
    # Check if user says "make the down payment" or "pay the down payment" (meaning full amount)
    if re.search(r"(?:make|pay|paid|cover)\s+(?:the\s+)?(?:full\s+)?down\s*payment", lowered):
        result["down_payment_paid"] = down_payment_amt if down_payment_amt > 0 else 0.0
        result["down_payment_is_full"] = True
    
    # Look for down payment amounts with number
    dp_match = re.search(
        r"(?:paid|pay|spent|put)?\s*\$?([0-9,.]+)\s*[kK]?\s*(?:for\s+)?(?:the\s+)?(?:down\s*payment|dp|down)",
        lowered
    )
    if not dp_match:
        dp_match = re.search(
            r"(?:down\s*payment|dp)\s*(?:of|is|was|:)?\s*\$?([0-9,.]+)\s*[kK]?",
            lowered
        )
    if dp_match:
        match_end = dp_match.end(1)
        val = _safe_parse_amount(dp_match.group(1), lowered, match_end)
        if val is not None:
            result["down_payment_paid"] = val
    
    # Look for closing costs amounts - more flexible patterns
    cc_patterns = [
        r"(?:paid|pay|spent|put)?\s*\$?([0-9,.]+)\s*[kK]?\s*(?:for\s+)?(?:the\s+)?closing\s*(?:cost)?s?",
        r"(?:closing\s*costs?)\s*(?:of|is|was|:)?\s*\$?([0-9,.]+)\s*[kK]?",
        r"closing\s+(?:cost)?s?\s+(?:of|is|was|:)\s*\$?([0-9,.]+)\s*[kK]?",
        r"closing\s+(?:cost)?s?\s+(?:come|comes|came)\s+(?:out\s+to|to)\s*\$?([0-9,.]+)\s*[kK]?",
        r"closing\s+(?:cost)?s?\s+(?:total|totaled|totals)\s*\$?([0-9,.]+)\s*[kK]?",
        r"\$?([0-9,.]+)\s*[kK]?\s+(?:for|toward|towards)\s+closing",
        r"(?:pay|paid)\s+\$?([0-9,.]+)\s*[kK]?\s+(?:for|toward|towards|in)\s+closing",
        r"(?:pay|paid)\s+(?:the\s+)?closing\s*(?:cost)?s?\s+(?:of)?\s*\$?([0-9,.]+)\s*[kK]?",
    ]
    
    for pattern in cc_patterns:
        cc_match = re.search(pattern, lowered)
        if cc_match:
            match_end = cc_match.end(1)
            val = _safe_parse_amount(cc_match.group(1), lowered, match_end)
            if val is not None:
                result["closing_costs_paid"] = val
                break
    
    # Look for earnest money amounts
    em_match = re.search(
        r"(?:paid|pay|spent)?\s*\$?([0-9,.]+)\s*[kK]?\s*(?:for\s+)?(?:earnest|emd)",
        lowered
    )
    if not em_match:
        em_match = re.search(
            r"(?:earnest|emd)\s*(?:of)?\s*\$?([0-9,.]+)\s*[kK]?",
            lowered
        )
    if em_match:
        match_end = em_match.end(1)
        val = _safe_parse_amount(em_match.group(1), lowered, match_end)
        if val is not None:
            result["earnest_money_paid"] = val
    
    # Fallback: if user says "130K downpayment and 22.5K closing"
    fallback_match = re.search(
        r"\$?([0-9,.]+)\s*[kK]?\s*(?:down\s*payment|downpayment|dp).*?(?:and)?\s*\$?([0-9,.]+)\s*[kK]?\s*(?:for\s+)?(?:closing|cc)",
        lowered
    )
    if fallback_match and "down_payment_paid" not in result:
        dp_end = fallback_match.start(1) + len(fallback_match.group(1))
        cc_end = fallback_match.start(2) + len(fallback_match.group(2))
        dp_val = _safe_parse_amount(fallback_match.group(1), lowered, dp_end)
        cc_val = _safe_parse_amount(fallback_match.group(2), lowered, cc_end)
        if dp_val is not None:
            result["down_payment_paid"] = dp_val
        if cc_val is not None:
            result["closing_costs_paid"] = cc_val
    
    return result


def _update_context_from_text(text: str, context: Dict[str, Any], down_payment_amt: float = 0.0) -> None:
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
    
    # Store paid amounts in context for follow-up questions
    paid_amounts = _extract_paid_amounts(text, down_payment_amt)
    if paid_amounts:
        context["paid_amounts"] = paid_amounts


def _build_cash_to_close_breakdown(
    inputs: MortgageInputs,
    down_payment_amt: float,
    loan_amount: float,
    pi_monthly: float,
    context: Dict[str, Any] | None = None,
) -> Tuple[str, List[Dict[str, str]]]:
    closing_low, closing_high = _range_from_percent(inputs.home_price, 0.02, 0.04)
    context = context or {}
    itemized = context.get("closing_costs_itemized", {})
    itemized_total = itemized.get("itemized_total")
    if itemized_total and itemized_total > 0:
        closing_low = itemized_total
        closing_high = itemized_total
    elif inputs.closing_costs_value > 0:
        closing_amt = (
            inputs.home_price * (inputs.closing_costs_value / 100.0)
            if inputs.closing_costs_is_percent
            else inputs.closing_costs_value
        )
        closing_low = closing_amt
        closing_high = closing_amt

    earnest_low, earnest_high = _range_from_percent(inputs.home_price, 0.01, 0.03)
    if inputs.earnest_money_value > 0:
        earnest_amt = (
            inputs.home_price * (inputs.earnest_money_value / 100.0)
            if inputs.earnest_money_is_percent
            else inputs.earnest_money_value
        )
        earnest_low = earnest_amt
        earnest_high = earnest_amt
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
        {"Entry": "Closing costs", "Cost": _format_currency_range(closing_low, closing_high)},
        {"Entry": "Prepaids + escrow", "Cost": _format_currency_range(prepaids_low, prepaids_high)},
        {"Entry": "Earnest money (credited)", "Cost": _format_currency_range(earnest_low, earnest_high)},
        {"Entry": "Buyer diligence", "Cost": _format_currency_range(diligence_low, diligence_high)},
        {"Entry": "Move-in / repair buffer", "Cost": _format_currency_range(repair_low, repair_high)},
        {"Entry": "Emergency fund (3–6 mo)", "Cost": _format_currency_range(emergency_low, emergency_high)},
        {"Entry": "Cash to close (after earnest)", "Cost": _format_currency_range(cash_to_close_low, cash_to_close_high)},
        {"Entry": "Recommended safe cash target", "Cost": _format_currency_range(safe_target_low, safe_target_high)},
    ]

    if itemized_total and itemized_total > 0:
        breakdown.insert(
            2,
            {"Entry": "  Buyer agent fee", "Cost": _format_currency(itemized.get("realtor_fee_amount", 0.0))},
        )
        breakdown.insert(
            3,
            {"Entry": "  Title / Escrow", "Cost": _format_currency(itemized.get("title_escrow_fees", 0.0))},
        )
        breakdown.insert(
            4,
            {"Entry": "  Loan origination", "Cost": _format_currency(itemized.get("loan_origination_fees", 0.0))},
        )
        breakdown.insert(
            5,
            {"Entry": "  Recording / transfer taxes", "Cost": _format_currency(itemized.get("recording_transfer_taxes", 0.0))},
        )
        inspection_total = itemized.get("inspection_total", 0.0) or 0.0
        breakdown.insert(
            6,
            {"Entry": "  Inspections (total)", "Cost": _format_currency(inspection_total)},
        )

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


def _build_leftover_response(
    inputs: MortgageInputs,
    down_payment_amt: float,
    loan_amount: float,
    context: Dict[str, Any],
    user_question: str,
) -> Tuple[str, List[Dict[str, str]]]:
    """Build a response specifically for 'leftover' or 'remaining budget' questions."""
    pi_monthly = monthly_pi_payment(
        loan_amount,
        inputs.annual_interest_rate_pct,
        inputs.loan_term_years,
    )
    
    # Get the estimated cost ranges
    closing_low, closing_high = _range_from_percent(inputs.home_price, 0.02, 0.04)
    itemized = context.get("closing_costs_itemized", {})
    itemized_total = itemized.get("itemized_total")
    if itemized_total and itemized_total > 0:
        closing_low = itemized_total
        closing_high = itemized_total
    elif inputs.closing_costs_value > 0:
        closing_amt = (
            inputs.home_price * (inputs.closing_costs_value / 100.0)
            if inputs.closing_costs_is_percent
            else inputs.closing_costs_value
        )
        closing_low = closing_amt
        closing_high = closing_amt

    earnest_low, earnest_high = _range_from_percent(inputs.home_price, 0.01, 0.03)
    if inputs.earnest_money_value > 0:
        earnest_amt = (
            inputs.home_price * (inputs.earnest_money_value / 100.0)
            if inputs.earnest_money_is_percent
            else inputs.earnest_money_value
        )
        earnest_low = earnest_amt
        earnest_high = earnest_amt
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
    
    # Get what the user says they've already paid
    paid_amounts = context.get("paid_amounts", {})
    dp_paid = paid_amounts.get("down_payment_paid", 0.0)
    cc_paid = paid_amounts.get("closing_costs_paid", 0.0)
    em_paid = paid_amounts.get("earnest_money_paid", 0.0)
    effective_down_payment_paid = dp_paid + em_paid
    
    total_already_paid = effective_down_payment_paid + cc_paid
    
    # Calculate what's remaining in each category
    # For closing costs: if they paid 22.5K and estimate is 15.3K-30.6K
    closing_remaining_low = max(0, closing_low - cc_paid)
    closing_remaining_high = max(0, closing_high - cc_paid)
    
    # For down payment: if they paid vs required
    dp_remaining = max(0, down_payment_amt - effective_down_payment_paid)
    
    # Prepaids/escrow still needed (unless included in their closing costs)
    prepaids_remaining_low = prepaids_low
    prepaids_remaining_high = prepaids_high
    
    # Calculate remaining cash needed
    remaining_cash_low = dp_remaining + closing_remaining_low + prepaids_remaining_low
    remaining_cash_high = dp_remaining + closing_remaining_high + prepaids_remaining_high
    
    # Subtract earnest money credit if applicable
    if em_paid > 0:
        remaining_cash_low = max(0, remaining_cash_low - em_paid)
        remaining_cash_high = max(0, remaining_cash_high - em_paid)
    
    # Build detailed breakdown showing what's paid vs remaining
    breakdown = [
        {"Entry": "💰 You've already paid", "Cost": ""},
        {"Entry": "  Down payment paid", "Cost": _format_currency(dp_paid) if dp_paid > 0 else "—"},
        {"Entry": "  Earnest money paid (credited)", "Cost": _format_currency(em_paid) if em_paid > 0 else "—"},
        {"Entry": "  Down payment covered (total)", "Cost": _format_currency(effective_down_payment_paid)},
        {"Entry": "  Closing costs paid", "Cost": _format_currency(cc_paid) if cc_paid > 0 else "—"},
        {"Entry": "  **Total paid**", "Cost": _format_currency(total_already_paid)},
        {"Entry": "", "Cost": ""},
        {"Entry": "📋 Still needed for closing", "Cost": ""},
        {"Entry": "  Down payment remaining", "Cost": _format_currency(dp_remaining) if dp_remaining > 0 else "✅ Covered"},
        {"Entry": "  Closing costs remaining", "Cost": _format_currency_range(closing_remaining_low, closing_remaining_high) if closing_remaining_high > 0 else "✅ Covered"},
        {"Entry": "  Prepaids + escrow", "Cost": _format_currency_range(prepaids_remaining_low, prepaids_remaining_high)},
        {"Entry": "  **Remaining cash to close**", "Cost": _format_currency_range(remaining_cash_low, remaining_cash_high)},
        {"Entry": "", "Cost": ""},
        {"Entry": "🎯 Post-closing reserves (recommended)", "Cost": ""},
        {"Entry": "  Buyer diligence", "Cost": _format_currency_range(diligence_low, diligence_high)},
        {"Entry": "  Move-in / repair buffer", "Cost": _format_currency_range(repair_low, repair_high)},
        {"Entry": "  Emergency fund (3–6 mo)", "Cost": _format_currency_range(emergency_low, emergency_high)},
    ]

    if itemized_total and itemized_total > 0:
        breakdown.insert(
            9,
            {"Entry": "  Closing costs (itemized)", "Cost": _format_currency(itemized_total)},
        )
        breakdown.insert(
            10,
            {"Entry": "    Buyer agent fee", "Cost": _format_currency(itemized.get("realtor_fee_amount", 0.0))},
        )
        breakdown.insert(
            11,
            {"Entry": "    Title / Escrow", "Cost": _format_currency(itemized.get("title_escrow_fees", 0.0))},
        )
        breakdown.insert(
            12,
            {"Entry": "    Loan origination", "Cost": _format_currency(itemized.get("loan_origination_fees", 0.0))},
        )
        breakdown.insert(
            13,
            {"Entry": "    Recording / transfer taxes", "Cost": _format_currency(itemized.get("recording_transfer_taxes", 0.0))},
        )
        breakdown.insert(
            14,
            {"Entry": "    Inspections (total)", "Cost": _format_currency(itemized.get("inspection_total", 0.0) or 0.0)},
        )
    
    # Determine closing budget status
    if cc_paid > 0:
        if cc_paid >= closing_high:
            closing_status = f"✅ Your {_format_currency(cc_paid)} closing costs payment **fully covers** the estimated range ({_format_currency_range(closing_low, closing_high)})."
        elif cc_paid >= closing_low:
            closing_status = f"✅ Your {_format_currency(cc_paid)} closing costs payment covers the **low estimate** ({_format_currency(closing_low)}). You may need up to {_format_currency(closing_high - cc_paid)} more if costs are on the high end."
        else:
            closing_status = f"⚠️ Your {_format_currency(cc_paid)} closing costs payment is **below** the estimated range ({_format_currency_range(closing_low, closing_high)}). You may need {_format_currency_range(closing_remaining_low, closing_remaining_high)} more."
    else:
        closing_status = f"Closing costs not specified. Estimated range: {_format_currency_range(closing_low, closing_high)}."
    
    # Determine down payment status
    if effective_down_payment_paid > 0:
        if effective_down_payment_paid >= down_payment_amt:
            dp_status = (
                f"✅ Your {_format_currency(effective_down_payment_paid)} down payment (including earnest money) "
                f"**fully covers** the required {_format_currency(down_payment_amt)}."
            )
        else:
            dp_status = (
                f"⚠️ Your {_format_currency(effective_down_payment_paid)} down payment (including earnest money) is "
                f"{_format_currency(dp_remaining)} **short** of the required {_format_currency(down_payment_amt)}."
            )
    else:
        dp_status = f"Down payment not specified. Required: {_format_currency(down_payment_amt)}."
    
    response = (
        "### 💵 Closing Budget Analysis\n\n"
        "Based on what you've told me you've paid:\n\n"
        f"**Down Payment:** {dp_status}\n\n"
        f"**Closing Costs:** {closing_status}\n\n"
        "---\n\n"
        "### 📊 What's Left to Budget\n\n"
        f"**Remaining cash needed at closing:**\n"
        f"- {_format_currency_range(remaining_cash_low, remaining_cash_high)}\n\n"
        f"**Prepaids & escrow** (required at closing):\n"
        f"- {_format_currency_range(prepaids_remaining_low, prepaids_remaining_high)}\n\n"
        "_Note: Prepaids include ~12 months insurance, 2–6 months property tax escrow, and prepaid interest._\n\n"
        "---\n\n"
        "### 🎯 Recommended Reserves After Closing\n\n"
        f"- Buyer diligence (inspection, appraisal): {_format_currency_range(diligence_low, diligence_high)}\n"
        f"- Move-in / repair buffer: {_format_currency_range(repair_low, repair_high)}\n"
        f"- Emergency fund (3–6 months): {_format_currency_range(emergency_low, emergency_high)}\n"
    )
    
    return response, breakdown


def _build_response(
    inputs: MortgageInputs,
    down_payment_amt: float,
    loan_amount: float,
    context: Dict[str, Any],
    user_question: str = "",
) -> Tuple[str, List[Dict[str, str]]]:
    # Detect question type and route to appropriate handler
    question_type = _detect_question_type(user_question)
    
    # Handle leftover/remaining budget questions specially
    if question_type == "leftover_calculation" and context.get("paid_amounts"):
        return _build_leftover_response(
            inputs, down_payment_amt, loan_amount, context, user_question
        )
    
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
        context,
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
                if pricing_key and pricing_key in get_llm_pricing_registry():
                    pricing = get_llm_pricing_registry()[pricing_key]
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
        with st.expander("Closing Costs Details (Buyer Side)", expanded=False):
            st.caption("Provide buyer-side closing costs to itemize estimates for the chatbot.")

            realtor_cols = st.columns([0.7, 0.3], gap="small")
            with realtor_cols[0]:
                realtor_fee_value = st.number_input(
                    "Buyer agent fee",
                    min_value=0.0,
                    value=float(st.session_state.get("mortgage_realtor_fee_value", 0.0)),
                    step=0.1,
                )
            with realtor_cols[1]:
                realtor_fee_unit = st.selectbox(
                    "Unit",
                    options=["%", "$"],
                    index=0
                    if st.session_state.get("mortgage_realtor_fee_unit", "%") == "%"
                    else 1,
                    key="mortgage_realtor_fee_unit",
                )

            realtor_fee_amount = (
                inputs.home_price * (realtor_fee_value / 100.0)
                if realtor_fee_unit == "%"
                else realtor_fee_value
            )

            title_escrow_fees = st.number_input(
                "Title / Escrow fees ($)",
                min_value=0.0,
                value=float(st.session_state.get("mortgage_title_escrow_fees", 0.0)),
                step=100.0,
            )
            loan_origination_fees = st.number_input(
                "Loan origination fees ($)",
                min_value=0.0,
                value=float(st.session_state.get("mortgage_loan_origination_fees", 0.0)),
                step=100.0,
            )
            recording_transfer_taxes = st.number_input(
                "Recording / transfer taxes ($)",
                min_value=0.0,
                value=float(st.session_state.get("mortgage_recording_transfer_taxes", 0.0)),
                step=100.0,
            )

            if "mortgage_inspection_log" not in st.session_state:
                st.session_state["mortgage_inspection_log"] = []

            inspection_cols = st.columns([1.2, 0.6, 0.3], gap="small")
            with inspection_cols[0]:
                inspection_label = st.text_input(
                    "Inspection label",
                    key="mortgage_inspection_label",
                    placeholder="Home, radon, WDI",
                )
            with inspection_cols[1]:
                inspection_amount = st.number_input(
                    "Amount ($)",
                    min_value=0.0,
                    step=25.0,
                    key="mortgage_inspection_amount",
                )
            with inspection_cols[2]:
                add_inspection = st.button("Add", key="mortgage_inspection_add")

            if add_inspection:
                label = st.session_state.get("mortgage_inspection_label", "").strip()
                if label:
                    st.session_state["mortgage_inspection_log"].append(
                        {
                            "Label": label,
                            "Amount": st.session_state.get("mortgage_inspection_amount", 0.0),
                        }
                    )
                    st.session_state["mortgage_inspection_label"] = ""
                    st.session_state["mortgage_inspection_amount"] = 0.0

            inspection_container = st.container(height=140)
            with inspection_container:
                if not st.session_state["mortgage_inspection_log"]:
                    st.caption("No inspections logged yet.")
                else:
                    for idx, row in enumerate(st.session_state["mortgage_inspection_log"]):
                        row_cols = st.columns([1.2, 0.6, 0.2, 0.2], gap="small")
                        with row_cols[0]:
                            st.text_input(
                                "Label",
                                value=row.get("Label", ""),
                                key=f"mortgage_inspection_label_{idx}",
                                label_visibility="collapsed",
                            )
                        with row_cols[1]:
                            st.number_input(
                                "Amount",
                                min_value=0.0,
                                step=25.0,
                                value=float(row.get("Amount", 0.0)),
                                key=f"mortgage_inspection_amount_{idx}",
                                label_visibility="collapsed",
                            )
                        with row_cols[2]:
                            save_clicked = st.button("💾", key=f"mortgage_inspection_save_{idx}")
                        with row_cols[3]:
                            delete_clicked = st.button("🗑️", key=f"mortgage_inspection_delete_{idx}")

                        if save_clicked:
                            st.session_state["mortgage_inspection_log"][idx]["Label"] = (
                                st.session_state.get(f"mortgage_inspection_label_{idx}", "").strip()
                            )
                            st.session_state["mortgage_inspection_log"][idx]["Amount"] = (
                                st.session_state.get(f"mortgage_inspection_amount_{idx}", 0.0)
                            )
                        if delete_clicked:
                            st.session_state["mortgage_inspection_log"] = [
                                item
                                for i, item in enumerate(st.session_state["mortgage_inspection_log"])
                                if i != idx
                            ]
                            st.rerun()

            inspection_total = sum(
                float(item.get("Amount", 0.0))
                for item in st.session_state.get("mortgage_inspection_log", [])
            )

            st.session_state["mortgage_realtor_fee_value"] = realtor_fee_value
            st.session_state["mortgage_title_escrow_fees"] = title_escrow_fees
            st.session_state["mortgage_loan_origination_fees"] = loan_origination_fees
            st.session_state["mortgage_recording_transfer_taxes"] = recording_transfer_taxes

            itemized_total = (
                realtor_fee_amount
                + title_escrow_fees
                + loan_origination_fees
                + recording_transfer_taxes
                + inspection_total
            )

            context["closing_costs_itemized"] = {
                "realtor_fee_value": realtor_fee_value,
                "realtor_fee_unit": realtor_fee_unit,
                "realtor_fee_amount": realtor_fee_amount,
                "title_escrow_fees": title_escrow_fees,
                "loan_origination_fees": loan_origination_fees,
                "recording_transfer_taxes": recording_transfer_taxes,
                "inspection_items": list(st.session_state.get("mortgage_inspection_log", [])),
                "inspection_total": inspection_total,
                "itemized_total": itemized_total,
            }

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
            prompt = _build_mortgage_llm_prompt(
                user_input=user_input,
                inputs=inputs,
                down_payment_amt=down_payment_amt,
                loan_amount=loan_amount,
                context=context,
            )
            model_id = st.session_state.get("mortgage_inference_profile")
            if not model_id:
                st.error("Select an inference profile to use the mortgage chatbot.")
                return

            llm_response = _invoke_mortgage_llm(prompt, model_id=model_id)
            raw_text = llm_response.get("raw", "")
            usage = llm_response.get("usage", {})
            parsed_payload = _try_extract_json(raw_text)

            response_text = raw_text.strip()
            breakdown = []
            parsed_context = {}
            if parsed_payload:
                response_text = str(parsed_payload.get("response_markdown", "")).strip() or response_text
                breakdown = parsed_payload.get("breakdown", []) or []
                parsed_context = parsed_payload.get("parsed", {}) or {}
                _update_context_from_llm(context, parsed_context)
                st.session_state["mortgage_chat_context"] = context
                question_type = str(parsed_context.get("question_type") or "").strip()
                if question_type:
                    breakdown = _filter_breakdown_by_question_type(breakdown, question_type)

            st.session_state["mortgage_chat_history"].append(
                {"role": "user", "content": user_input}
            )
            st.session_state["mortgage_chat_history"].append(
                {"role": "assistant", "content": response_text}
            )
            st.session_state["mortgage_cost_breakdown"] = breakdown

            input_tokens = usage.get("input_tokens") or max(1, len(prompt.split()))
            output_tokens = usage.get("output_tokens") or max(20, len(response_text.split()))
            _record_cost(int(input_tokens), int(output_tokens))
            st.rerun()

        with st.expander("Chat History", expanded=False):
            chat_container = st.container(height=260)
            with chat_container:
                if not st.session_state["mortgage_chat_history"]:
                    st.info("Ask: 'What cash do I need to close?' or 'Check my down payment'.")
                for msg in st.session_state["mortgage_chat_history"]:
                    with st.chat_message(msg["role"]):
                        st.markdown(_normalize_history_markdown(msg["content"]))