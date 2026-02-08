"""Consolidated AI usage cost tracking across all chatbots and features."""

from __future__ import annotations

import streamlit as st


def get_total_usage_stats() -> dict:
    """Aggregate usage statistics from all AI-powered features."""
    
    # Checklist Assistant costs
    assistant_records = st.session_state.get("assistant_cost_records", [])
    assistant_input = sum(r.get("input_tokens", 0) for r in assistant_records)
    assistant_output = sum(r.get("output_tokens", 0) for r in assistant_records)
    assistant_cost = sum(r.get("estimated_cost_usd", 0.0) for r in assistant_records)
    
    # Mortgage Chatbot costs
    mortgage_records = st.session_state.get("mortgage_cost_records", [])
    mortgage_input = sum(r.get("input_tokens", 0) for r in mortgage_records)
    mortgage_output = sum(r.get("output_tokens", 0) for r in mortgage_records)
    mortgage_cost = sum(r.get("estimated_cost_usd", 0.0) for r in mortgage_records)
    
    # HOA Document Vetting costs
    hoa_records = st.session_state.get("hoa_cost_records", [])
    hoa_input = sum(r.get("input_tokens", 0) for r in hoa_records)
    hoa_output = sum(r.get("output_tokens", 0) for r in hoa_records)
    hoa_cost = sum(r.get("estimated_cost_usd", 0.0) for r in hoa_records)
    
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
    
    with st.sidebar.expander("💰 Usage Costs", expanded=False):
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
