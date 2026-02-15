"""UI helpers for the floating chatbot and checklist/notes section."""

from __future__ import annotations

import re
from typing import List, Dict, Any

import uuid
from datetime import date

import streamlit as st

from config.pricing import (
    CLAUDE_INFERENCE_PROFILES,
    estimate_request_cost,
    get_llm_pricing_registry,
    get_pricing_version,
)
from profile.costs import _recalculate_costs
from profile.state_io import auto_save_profile

STOPWORDS = {
    "a",
    "an",
    "the",
    "to",
    "for",
    "of",
    "with",
    "on",
    "in",
    "this",
    "that",
    "update",
    "set",
    "change",
    "add",
    "delete",
    "remove",
    "notes",
    "note",
    "section",
    "checklist",
    "item",
    "row",
    "due",
    "date",
    "status",
    "category",
}

MONTHS = {
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
}

ALIASES = {
    "covenant review": "HOA Covenant Review",
    "hoa covenant review": "HOA Covenant Review",
}


DEFAULT_CHECKLIST: List[Dict[str, object]] = [
    {
        "label": "Get pre-approved by a lender",
        "status": "not_started",
        "due_date": None,
        "category": "Financing",
        "notes": "",
    },
    {
        "label": "Choose a loan officer",
        "status": "not_started",
        "due_date": None,
        "category": "Financing",
        "notes": "",
    },
    {
        "label": "Pull credit reports",
        "status": "not_started",
        "due_date": None,
        "category": "Financing",
        "notes": "",
    },
    {
        "label": "Hire a buyer's agent",
        "status": "not_started",
        "due_date": None,
        "category": "Representation",
        "notes": "",
    },
    {
        "label": "Schedule a home inspection",
        "status": "not_started",
        "due_date": None,
        "category": "Inspection",
        "notes": "",
    },
    {
        "label": "Order an appraisal",
        "status": "not_started",
        "due_date": None,
        "category": "Inspection",
        "notes": "",
    },
    {
        "label": "Engage a real estate attorney",
        "status": "not_started",
        "due_date": None,
        "category": "Legal",
        "notes": "",
    },
    {
        "label": "Budget for closing costs",
        "status": "not_started",
        "due_date": None,
        "category": "Closing",
        "notes": "",
    },
    {
        "label": "Review homeowners insurance options",
        "status": "not_started",
        "due_date": None,
        "category": "Insurance",
        "notes": "",
    },
    {
        "label": "HOA Covenant Review",
        "status": "not_started",
        "due_date": None,
        "category": "Legal",
        "notes": "",
    },
]


def _ensure_assistant_state() -> None:
    if "assistant_checklist" not in st.session_state:
        st.session_state["assistant_checklist"] = [item.copy() for item in DEFAULT_CHECKLIST]
    if "assistant_notes" not in st.session_state:
        st.session_state["assistant_notes"] = ""
    if "assistant_selected_label" not in st.session_state:
        st.session_state["assistant_selected_label"] = None
    if "assistant_chat_history" not in st.session_state:
        st.session_state["assistant_chat_history"] = []
    if "assistant_cost_records" not in st.session_state:
        st.session_state["assistant_cost_records"] = []
    if "assistant_inference_profile" not in st.session_state:
        st.session_state["assistant_inference_profile"] = None
    if "assistant_profile_initialized" not in st.session_state:
        st.session_state["assistant_profile_initialized"] = False
    if "assistant_pending_actions" not in st.session_state:
        st.session_state["assistant_pending_actions"] = None


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
    profile_id = st.session_state.get("assistant_inference_profile")
    model_key = _get_pricing_key_for_profile(profile_id or "")
    registry = get_llm_pricing_registry()
    if not model_key or model_key not in registry:
        return
    estimated_cost = estimate_request_cost(model_key, input_tokens, output_tokens)
    st.session_state["assistant_cost_records"].append(
        {
            "request_id": str(uuid.uuid4()),
            "model_id": model_key,
            "inference_profile": profile_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "pricing_version": get_pricing_version(),
            "timestamp": date.today().isoformat(),
        }
    )
    # Auto-save profile to persist costs
    auto_save_profile()


def _parse_date(text: str) -> str | None:
    """Extract a date from text like 'August 01 2026' or '2026-08-01'."""
    import calendar
    month_names = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}
    month_abbr = {m.lower(): i for i, m in enumerate(calendar.month_abbr) if m}
    all_months = {**month_names, **month_abbr}

    # Try "Month DD YYYY"
    pattern = r"(\w+)\s+(\d{1,2}),?\s*(\d{4})"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        month_str, day_str, year_str = match.groups()
        month_num = all_months.get(month_str.lower())
        if month_num:
            return f"{year_str}-{month_num:02d}-{int(day_str):02d}"

    # Try ISO format
    iso_match = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso_match:
        return iso_match.group(0)

    return None


def _tokenize(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text.lower()))
    return {
        token
        for token in tokens
        if token not in STOPWORDS and token not in MONTHS and not token.isdigit()
    }


def _extract_notes(text: str) -> str | None:
    note_match = re.search(
        r"notes?\s*(?:section)?\s*(?:for|on)?\s*.+?\s*(?:with|:)\s*['\"](.+?)['\"]",
        text,
        re.IGNORECASE,
    )
    if note_match:
        return note_match.group(1).strip()
    message_match = re.search(
        r"notes?\s*(?:section)?\s*(?:for|on)?\s*.+?\s*with\s+the\s+following\s+message\s*['\"](.+?)['\"]",
        text,
        re.IGNORECASE,
    )
    if message_match:
        return message_match.group(1).strip()
    return None


def _infer_category(text: str) -> str:
    lowered = text.lower()
    if "insurance" in lowered:
        return "Insurance"
    if "legal" in lowered or "attorney" in lowered or "covenant" in lowered:
        return "Legal"
    if "inspection" in lowered or "appraisal" in lowered:
        return "Inspection"
    if "loan" in lowered or "credit" in lowered or "pre-approv" in lowered:
        return "Financing"
    if "closing" in lowered:
        return "Closing"
    if "agent" in lowered or "buyer" in lowered:
        return "Representation"
    return ""


def _infer_label_from_prompt(text: str) -> str:
    raw_tokens = [
        token.capitalize()
        for token in _tokenize(text)
        if token not in {"yes", "no"}
    ]
    return " ".join(sorted(raw_tokens, key=str.lower)).strip()


def _find_checklist_match(text: str) -> dict | None:
    """Find a checklist item that matches the user's text."""
    lowered = re.sub(r"['\"].+?['\"]", " ", text.lower())
    checklist = st.session_state.get("assistant_checklist", [])

    if "this" in lowered:
        selected_label = st.session_state.get("assistant_selected_label")
        if selected_label:
            for item in checklist:
                if item.get("label") == selected_label:
                    return item

    for alias, label in ALIASES.items():
        if alias in lowered:
            for item in checklist:
                if item.get("label") == label:
                    return item

    best_item = None
    best_score = 0
    prompt_tokens = _tokenize(lowered)
    for item in checklist:
        label = str(item.get("label", ""))
        label_tokens = _tokenize(label)
        if not label_tokens or not prompt_tokens:
            continue
        overlap = label_tokens.intersection(prompt_tokens)
        score = len(overlap)
        if score > best_score:
            best_score = score
            best_item = item

    return best_item if best_score >= 2 else None


def _simulate_bedrock_response(user_text: str) -> dict[str, Any]:
    """Simulate a Bedrock response that parses user intent and returns actions."""
    actions = []
    lowered = user_text.lower()

    notes_text = _extract_notes(user_text)
    inferred_category = _infer_category(user_text)
    action_type = "update"
    if re.search(r"\b(add|create)\b", lowered):
        action_type = "add"
    elif re.search(r"\b(delete|remove)\b", lowered):
        action_type = "delete"

    add_match = re.search(
        r"\badd\b\s+(?:the\s+following\s+)?(?:checklist\s+)?(?:item|row)?\s*(?:named|called|titled)?\s*\"?(.+?)\"?$",
        lowered,
    )
    if action_type == "add":
        label_text = add_match.group(1).strip().strip('"').title() if add_match else ""
        if not label_text:
            label_text = _infer_label_from_prompt(lowered)
        if label_text:
            actions.append(
                {
                    "type": "add",
                    "label": label_text,
                    "status": "not_started",
                    "due_date": _parse_date(user_text),
                    "category": inferred_category,
                    "notes": notes_text or "",
                }
            )
            return {
                "response": (
                    f"I can add **{label_text}** to your checklist. "
                    "Use the buttons below to confirm or cancel."
                ),
                "actions": actions,
                "apply_now": False,
                "input_tokens": max(1, len(user_text.split())),
                "output_tokens": 14,
            }

    # Confirmation is handled via buttons, not chat replies.

    # Parse user intent for checklist updates
    matched_item = _find_checklist_match(user_text)
    parsed_date = _parse_date(user_text)

    if matched_item:
        action = {
            "type": "update",
            "label": matched_item["label"],
            "status": matched_item.get("status", "not_started"),
            "category": matched_item.get("category", ""),
        }

        if notes_text:
            action["notes"] = notes_text

        # Check for status changes
        if "done" in lowered or "complete" in lowered or "finished" in lowered:
            action["status"] = "done"
        elif "progress" in lowered or "start" in lowered or "working" in lowered:
            action["status"] = "in_progress"

        # Check for date changes
        if parsed_date:
            action["due_date"] = parsed_date

        actions.append(action)

        # Build response
        changes = []
        if action.get("due_date"):
            changes.append(f"due date to {action['due_date']}")
        if action["status"] != matched_item.get("status"):
            changes.append(f"status to '{action['status']}'")

        if action.get("notes"):
            changes.append("notes")

        if changes:
            response_text = (
                f"I can update **{matched_item['label']}**: {', '.join(changes)}.\n\n"
                "Use the buttons below to confirm or cancel."
            )
        else:
            response_text = (
                f"I found **{matched_item['label']}**. "
                "What would you like to change? (status, due date, notes)"
            )
            actions = []  # No action if nothing to change
    else:
        inferred_label = _infer_label_from_prompt(lowered)
        if inferred_label:
            actions.append(
                {
                    "type": "add",
                    "label": inferred_label,
                    "status": "not_started",
                    "due_date": parsed_date,
                    "category": inferred_category,
                    "notes": notes_text or "",
                }
            )
            response_text = (
                "I couldn't find a matching checklist item. "
                f"I can add **{inferred_label}** with the requested details. "
                "Use the buttons below to confirm or cancel."
            )
        else:
            response_text = (
                "I couldn't find a matching checklist item. "
                "Would you like me to add it as a new item? "
                "Try: \"Add checklist item 'Your Item Name'\"."
            )

    return {
        "response": response_text,
        "actions": actions,
        "apply_now": False,
        "input_tokens": max(1, len(user_text.split())),
        "output_tokens": max(10, len(response_text.split())),
    }


def _apply_actions(actions: list[dict[str, Any]]) -> None:
    checklist = st.session_state.get("assistant_checklist", [])
    for action in actions:
        if action.get("type") == "add":
            checklist.append(
                {
                    "label": action.get("label", "New Item"),
                    "status": action.get("status", "not_started"),
                    "due_date": action.get("due_date"),
                    "category": action.get("category", ""),
                    "notes": action.get("notes", ""),
                }
            )
            continue
        label = action.get("label")
        for item in checklist:
            if item.get("label") == label:
                if action.get("status"):
                    item["status"] = action["status"]
                if action.get("category"):
                    item["category"] = action["category"]
                if action.get("due_date"):
                    item["due_date"] = action["due_date"]
                if action.get("notes"):
                    item["notes"] = action["notes"]
    st.session_state["assistant_checklist"] = checklist
    st.session_state["assistant_pending_actions"] = None


def render_checklist_and_notes() -> None:
    _ensure_assistant_state()

    with st.expander("Home Buying Checklist & Notes", expanded=False):
        st.caption(
            "Track next steps toward purchasing a home. Update checklist items,"
            " and keep key notes handy during your search."
        )

        checklist = st.session_state["assistant_checklist"]
        if not st.session_state.get("assistant_selected_label") and checklist:
            st.session_state["assistant_selected_label"] = checklist[0].get("label")

        checklist_box = st.container(border=True)
        with checklist_box:
            st.markdown("### Checklist")
            if not checklist:
                st.info("No checklist items yet. Add one below.")

            st.markdown(
                """
                <style>
                [data-testid="stDataEditor"] table td {
                    white-space: normal !important;
                }
                </style>
                """,
                unsafe_allow_html=True,
            )

            # Convert string dates to date objects for the data editor
            checklist_df = []
            for item in st.session_state.get("assistant_checklist", []):
                row = item.copy()
                if row.get("due_date") and isinstance(row["due_date"], str):
                    try:
                        row["due_date"] = date.fromisoformat(row["due_date"])
                    except ValueError:
                        row["due_date"] = None
                row["selected"] = row.get("label") == st.session_state.get("assistant_selected_label")
                checklist_df.append(row)
            checklist_editor = st.data_editor(
                checklist_df,
                num_rows="dynamic",
                width='stretch',
                column_config={
                    "selected": st.column_config.CheckboxColumn(
                        "Select",
                        width="small",
                    ),
                    "label": st.column_config.TextColumn(
                        "Checklist Item",
                    ),
                    "status": st.column_config.SelectboxColumn(
                        "Status",
                        options=["not_started", "in_progress", "done"],
                    ),
                    "category": st.column_config.TextColumn(
                        "Category",
                    ),
                    "due_date": st.column_config.DateColumn(
                        "Due Date",
                    ),
                    "notes": st.column_config.TextColumn(
                        "Notes",
                    ),
                },
                key="assistant_checklist_editor",
            )

            updated_rows = []
            selected_label = None
            for row in checklist_editor:
                if str(row.get("label", "")).strip():
                    if row.get("selected"):
                        selected_label = row.get("label")
                    updated_rows.append(
                        {
                            "label": str(row.get("label", "")).strip(),
                            "status": row.get("status", "not_started"),
                            "due_date": row.get("due_date"),
                            "category": str(row.get("category", "")),
                            "notes": str(row.get("notes", "")),
                        }
                    )
            st.session_state["assistant_checklist"] = updated_rows
            if selected_label:
                st.session_state["assistant_selected_label"] = selected_label

        selected_item = None
        for item in st.session_state["assistant_checklist"]:
            if item.get("label") == st.session_state.get("assistant_selected_label"):
                selected_item = item
                break


        notes_box = st.container(border=True)
        with notes_box:
            notes_value = (
                selected_item.get("notes", "")
                if selected_item
                else st.session_state.get("assistant_notes", "")
            )
            updated_notes = st.text_area(
                "Notes",
                value=notes_value,
                height=160,
                label_visibility="collapsed",
                placeholder="Capture lender contacts, questions, appointment notes, etc.",
            )
            if selected_item:
                selected_item["notes"] = updated_notes
            else:
                st.session_state["assistant_notes"] = updated_notes

        with st.expander("Home Buying Assistant", expanded=False):
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
                default_profile_id = st.session_state.get("assistant_inference_profile")
                if not st.session_state.get("assistant_profile_initialized"):
                    default_profile_id = next(iter(profile_options.keys()))
                    st.session_state["assistant_inference_profile"] = default_profile_id
                    st.session_state["assistant_profile_initialized"] = True
                elif default_profile_id not in profile_options:
                    default_profile_id = next(iter(profile_options.keys()))
                selected_profile = st.selectbox(
                    "Select Model",
                    options=list(profile_options.keys()),
                    format_func=lambda pid: profile_options[pid],
                    index=list(profile_options.keys()).index(default_profile_id),
                )
                st.session_state["assistant_inference_profile"] = selected_profile

            _render_chat_assistant()


def _render_chat_assistant() -> None:
    """Render the assistant chat inside the checklist expander."""
    _ensure_assistant_state()

    # Calculate usage totals
    total_input, total_output, total_cost = _recalculate_costs(
        st.session_state.get("assistant_cost_records", [])
    )

    st.caption(f"Usage: {total_input} in / {total_output} out · ${total_cost:.6f}")

    # Display chat history
    chat_container = st.container(height=300)
    with chat_container:
        if not st.session_state["assistant_chat_history"]:
            st.info("Ask me to update your checklist! Try: 'Set pre-approval due date to August 1, 2026'")

        for msg in st.session_state["assistant_chat_history"]:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])

    # Chat input
    user_input = st.chat_input("Ask about your checklist...", key="assistant_chat_input")

    if user_input:
        # Add user message to history
        st.session_state["assistant_chat_history"].append(
            {"role": "user", "content": user_input}
        )

        # Get assistant response
        result = _simulate_bedrock_response(user_input)

        # Record cost
        _record_cost(result["input_tokens"], result["output_tokens"])

        # Apply actions immediately if confirmed
        if result.get("apply_now") and result.get("actions"):
            _apply_actions(result["actions"])
        elif result.get("actions"):
            # Store pending actions for confirmation
            st.session_state["assistant_pending_actions"] = result["actions"]

        # Add assistant response to history
        st.session_state["assistant_chat_history"].append(
            {"role": "assistant", "content": result["response"]}
        )

        # Rerun to update UI
        st.rerun()

    # Show pending actions if any
    pending_actions = st.session_state.get("assistant_pending_actions")
    if pending_actions:
        st.markdown("#### Proposed Changes")
        for action in pending_actions:
            if action.get("type") == "add":
                st.write(
                    "- **add**: "
                    f"label={action.get('label')} | "
                    f"status={action.get('status', 'not_started')} | "
                    f"category={action.get('category', '') or '—'} | "
                    f"due_date={action.get('due_date') or '—'}"
                )
            else:
                st.write(
                    "- **update**: "
                    f"label={action.get('label')} | "
                    f"status={action.get('status', 'unchanged')} | "
                    f"category={action.get('category', 'unchanged')} | "
                    f"due_date={action.get('due_date') or 'unchanged'}"
                )
        action_cols = st.columns([0.2, 0.2, 0.6])
        if action_cols[0].button("Confirm", key="assistant_confirm"):
            summary_lines = []
            for action in pending_actions:
                if action.get("type") == "add":
                    summary_lines.append(
                        "Added item: "
                        f"{action.get('label')} "
                        f"(status={action.get('status', 'not_started')}, "
                        f"category={action.get('category') or '—'}, "
                        f"due_date={action.get('due_date') or '—'})"
                    )
                else:
                    summary_lines.append(
                        "Updated item: "
                        f"{action.get('label')} "
                        f"(status={action.get('status', 'unchanged')}, "
                        f"category={action.get('category', 'unchanged')}, "
                        f"due_date={action.get('due_date') or 'unchanged'})"
                    )
            _apply_actions(pending_actions)
            st.session_state["assistant_pending_actions"] = None
            st.session_state["assistant_chat_history"].append(
                {
                    "role": "assistant",
                    "content": "\n".join(summary_lines),
                }
            )
            st.success("Checklist updated.")
            st.rerun()
        if action_cols[1].button("Cancel", key="assistant_cancel"):
            st.session_state["assistant_pending_actions"] = None
            st.info("No changes applied.")
            st.rerun()


def render_floating_chatbot() -> None:
    """Deprecated: kept for compatibility when called in app.py."""
    return None
