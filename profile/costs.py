"""Consolidated AI usage cost tracking across all chatbots and features."""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

from config.pricing import (
    estimate_request_cost,
    get_api_pricing_entries,
    get_llm_pricing_registry,
    get_pricing_version,
)
from profile.state_io import auto_save_profile


def _recalculate_costs(records: list[dict]) -> tuple[float, float, float]:
    total_input = sum(record.get("input_tokens", 0) for record in records)
    total_output = sum(record.get("output_tokens", 0) for record in records)
    registry = get_llm_pricing_registry()
    total_cost = 0.0
    for record in records:
        model_id = record.get("model_id")
        model_key = record.get("model_key") or model_id
        if not model_key or model_key not in registry:
            total_cost += record.get("estimated_cost_usd", 0.0)
            continue
        total_cost += estimate_request_cost(
            model_key,
            int(record.get("input_tokens", 0)),
            int(record.get("output_tokens", 0)),
        )
    return total_input, total_output, total_cost


def _api_pricing_lookup() -> dict[str, dict]:
    lookup: dict[str, dict] = {}
    for entry in get_api_pricing_entries():
        service_key = entry.get("service_key") or entry.get("service") or entry.get("url")
        if not service_key:
            continue
        lookup[service_key] = entry
    return lookup


def record_api_usage(
    *,
    service_key: str,
    url: str,
    requests: int = 1,
    metadata: dict | None = None,
) -> None:
    """Record an external API usage event for centralized cost tracking."""
    if requests <= 0:
        return
    if "api_usage_records" not in st.session_state:
        st.session_state["api_usage_records"] = []
    st.session_state["api_usage_records"].append(
        {
            "service_key": service_key,
            "url": url,
            "requests": int(requests),
            "metadata": metadata or {},
            "pricing_version": get_pricing_version(),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    auto_save_profile()


def _recalculate_api_usage(records: list[dict]) -> dict:
    lookup = _api_pricing_lookup()
    totals = {
        "requests": 0,
        "cost_usd": 0.0,
        "services": {},
    }
    for record in records:
        service_key = record.get("service_key") or record.get("service") or record.get("url")
        requests = int(record.get("requests", 0))
        if not service_key or requests <= 0:
            continue
        entry = lookup.get(service_key)
        cost_per_request = float(entry.get("cost_per_request_usd", 0.0)) if entry else 0.0
        service_label = entry.get("service") if entry else service_key
        url = entry.get("url") if entry else record.get("url")
        free_tier = entry.get("free_tier") if entry else None
        free_requests = 0
        if isinstance(free_tier, dict):
            free_requests = int(free_tier.get("requests", 0) or 0)
        service_stats = totals["services"].setdefault(
            service_key,
            {
                "service_key": service_key,
                "service": service_label,
                "url": url,
                "requests": 0,
                "billable_requests": 0,
                "free_tier_requests": free_requests,
                "cost_usd": 0.0,
                "cost_per_request_usd": cost_per_request,
            },
        )
        service_stats["requests"] += requests
        billable_requests = max(service_stats["requests"] - free_requests, 0)
        incremental_billable = max(billable_requests - service_stats["billable_requests"], 0)
        cost = incremental_billable * cost_per_request
        service_stats["billable_requests"] = billable_requests
        service_stats["cost_usd"] += cost
        totals["requests"] += requests
        totals["cost_usd"] += cost
    return totals


def _api_usage_record_details(record: dict, pricing: dict | None) -> dict:
    details = {
        "service_key": record.get("service_key") or record.get("service"),
        "url": record.get("url"),
        "requests": record.get("requests"),
        "timestamp": record.get("timestamp"),
        "metadata": record.get("metadata") or {},
    }
    if pricing:
        details["cost_per_request_usd"] = pricing.get("cost_per_request_usd")
    return details


def _lookup_label(record: dict) -> str | None:
    metadata = record.get("metadata") or {}
    lookup = metadata.get("lookup")
    if not lookup:
        return None
    return str(lookup).replace("_", " ").title()


def get_total_usage_stats() -> dict:
    """Aggregate usage statistics from all AI-powered features."""
    
    # Checklist Assistant costs
    assistant_records = st.session_state.get("assistant_cost_records", [])
    assistant_input, assistant_output, assistant_cost = _recalculate_costs(assistant_records)
    
    # Mortgage Chatbot costs
    mortgage_records = st.session_state.get("mortgage_cost_records", [])
    mortgage_input, mortgage_output, mortgage_cost = _recalculate_costs(mortgage_records)
    
    # HOA Document Vetting costs
    hoa_records = st.session_state.get("hoa_cost_records", [])
    hoa_input, hoa_output, hoa_cost = _recalculate_costs(hoa_records)
    
    return {
        "checklist_assistant": {
            "name": "Checklist Assistant",
            "input_tokens": assistant_input,
            "output_tokens": assistant_output,
            "cost_usd": assistant_cost,
            "request_count": len(assistant_records),
        },
        "mortgage_chatbot": {
            "name": "Mortgage & Loan Chatbot",
            "input_tokens": mortgage_input,
            "output_tokens": mortgage_output,
            "cost_usd": mortgage_cost,
            "request_count": len(mortgage_records),
        },
        "document_vetting": {
            "name": "HOA Document Vetting",
            "input_tokens": hoa_input,
            "output_tokens": hoa_output,
            "cost_usd": hoa_cost,
            "request_count": len(hoa_records),
        },
        "totals": {
            "input_tokens": assistant_input + mortgage_input + hoa_input,
            "output_tokens": assistant_output + mortgage_output + hoa_output,
            "cost_usd": assistant_cost + mortgage_cost + hoa_cost,
            "request_count": len(assistant_records) + len(mortgage_records) + len(hoa_records),
        },
    }


def render_usage_costs() -> None:
    """Render the consolidated AI usage costs section below profile manager."""
    
    stats = get_total_usage_stats()
    totals = stats["totals"]
    
    # Only show if there's any usage
    has_usage = totals["request_count"] > 0
    
    with st.sidebar.expander("💰 LLM Usage Costs", expanded=False):
        if not has_usage:
            st.caption("No AI usage recorded yet.")
            st.markdown(
                "Costs will appear here as you use:\n\n"
                "- Checklist Assistant\n"
                "- Mortgage & Loan Chatbot\n"
                "- HOA Document Vetting"
            )
            return
        
        # Summary totals
        st.markdown("### Session Totals")
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Total Cost", f"${totals['cost_usd']:.4f}")
        with col2:
            st.metric("Requests", totals["request_count"])
        
        st.caption(
            f"Tokens: {totals['input_tokens']:,} in / {totals['output_tokens']:,} out"
        )
        
        st.divider()
        
        # Breakdown by feature
        st.markdown("### By Feature")
        
        for key in ["checklist_assistant", "mortgage_chatbot", "document_vetting"]:
            feature = stats[key]
            if feature["request_count"] > 0:
                with st.container(border=True):
                    st.markdown(f"**{feature['name']}**")
                    st.caption(
                        f"${feature['cost_usd']:.4f} · "
                        f"{feature['request_count']} request{'s' if feature['request_count'] != 1 else ''} · "
                        f"{feature['input_tokens']:,} in / {feature['output_tokens']:,} out"
                    )


def render_api_usage_costs() -> None:
    """Render a dedicated API usage cost breakdown in the sidebar."""
    records = st.session_state.get("api_usage_records", [])
    stats = _recalculate_api_usage(records)
    pricing_lookup = _api_pricing_lookup()
    with st.sidebar.expander("🌐 API Usage Costs", expanded=False):
        if not records or stats["requests"] == 0:
            st.caption("No external API usage recorded yet.")
            st.markdown(
                "Usage costs will appear here for tracked APIs like Waze, Zillow, and Google Maps."
            )
            return

        st.markdown("### Session Totals")
        col1, col2 = st.columns(2)
        with col1:
            st.metric("Total Cost", f"${stats['cost_usd']:.4f}")
        with col2:
            st.metric("Requests", stats["requests"])

        st.divider()
        st.markdown("### By API")
        for service in sorted(
            stats["services"].values(),
            key=lambda item: item.get("cost_usd", 0.0),
            reverse=True,
        ):
            label = (
                f"{service['service']} · {service['requests']} request"
                f"{'s' if service['requests'] != 1 else ''}"
            )
            with st.expander(label, expanded=False):
                st.caption(
                    f"${service['cost_usd']:.4f} · "
                    f"${service['cost_per_request_usd']:.4f}/request"
                )
                free_tier = service.get("free_tier_requests", 0)
                billable = service.get("billable_requests", service["requests"])
                if free_tier:
                    st.caption(
                        f"Free-tier applied: {free_tier} requests free · "
                        f"{billable} billable"
                    )
                if service.get("url"):
                    st.caption(service["url"])

        st.divider()
        st.markdown("### Request Details")
        for record in sorted(records, key=lambda item: item.get("timestamp") or "", reverse=True):
            service_key = record.get("service_key") or record.get("service") or record.get("url")
            pricing = pricing_lookup.get(service_key) if service_key else None
            service_label = pricing.get("service") if pricing else service_key or "Unknown Service"
            requests = int(record.get("requests", 0))
            cost_per_request = float(pricing.get("cost_per_request_usd", 0.0)) if pricing else 0.0
            estimated_cost = requests * cost_per_request
            timestamp = record.get("timestamp") or "Unknown time"
            lookup_label = _lookup_label(record)
            lookup_segment = f" · {lookup_label}" if lookup_label else ""
            expander_label = (
                f"{timestamp} · {service_label}{lookup_segment} · {requests} request"
                f"{'s' if requests != 1 else ''}"
            )
            with st.expander(expander_label, expanded=False):
                st.caption(f"Estimated cost: ${estimated_cost:.4f}")
                details = _api_usage_record_details(record, pricing)
                st.json(details)
