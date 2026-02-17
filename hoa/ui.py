"""Streamlit UI for HOA document vetting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List
import os
import re
import json
import time

import streamlit as st

from config.pricing import (
    CLAUDE_INFERENCE_PROFILES,
    estimate_request_cost,
    get_llm_pricing_registry,
    get_pricing_version,
)
from profile.costs import _recalculate_costs, record_document_operation
from hoa.analysis import (
    analyze_document_chunked,
    analyze_document_chunked_green,
    answer_question_chunked,
)
from hoa.extraction import (
    build_page_context,
    DocumentExtraction,
    start_textract_job,
    start_textract_job_for_s3_key,
    poll_textract_job,
    cleanup_textract_job,
    blocks_to_extraction,
    extraction_to_payload,
    payload_to_extraction,
)
from profile.identity import (
    get_owner_sub,
    bucket_name_for_owner,
    get_storage_bucket_prefix,
)
from profile.state_io import auto_save_profile
import boto3

S3_STORAGE_PER_GB_MONTH = 0.023
S3_STORAGE_PER_GB_MONTH_STANDARD_IA = 0.0125
S3_STORAGE_PER_GB_MONTH_ONE_ZONE_IA = 0.01
S3_PUT_REQUEST_PER_1000 = 0.005
S3_GET_REQUEST_PER_1000 = 0.0004
TEXTRACT_TEXT_DETECTION_PER_PAGE = 0.0015
GB_IN_BYTES = 1024 ** 3
HOURS_IN_MONTH = 720.0
S3_STAGING_HOURS = 0.25
HOURS_IN_DAY = 24.0
HOURS_IN_WEEK = 24.0 * 7
HOURS_IN_YEAR = 24.0 * 365
MAX_UPLOAD_MB = 50
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024


@dataclass
class VettingQuery:
    question: str
    answer: str
    document_name: str
    page_numbers: List[int]
    quoted_text: str
    confidence: str = "low"
    not_found: bool = False
    answer_type: str = "Summary"
    page_range: tuple[int, int] | None = None


def _ensure_vetting_state() -> None:
    if "hoa_documents" not in st.session_state:
        st.session_state["hoa_documents"] = []
    if "hoa_extraction" not in st.session_state:
        st.session_state["hoa_extraction"] = None
    if "hoa_analysis" not in st.session_state:
        st.session_state["hoa_analysis"] = None
    if "hoa_green_analysis" not in st.session_state:
        st.session_state["hoa_green_analysis"] = None
    if "hoa_queries" not in st.session_state:
        st.session_state["hoa_queries"] = []
    if "hoa_cost_records" not in st.session_state:
        st.session_state["hoa_cost_records"] = []
    if "hoa_inference_profile" not in st.session_state:
        st.session_state["hoa_inference_profile"] = None
    if "hoa_profile_initialized" not in st.session_state:
        st.session_state["hoa_profile_initialized"] = False
    if "hoa_textract_job_id" not in st.session_state:
        st.session_state["hoa_textract_job_id"] = None
    if "hoa_textract_s3_key" not in st.session_state:
        st.session_state["hoa_textract_s3_key"] = None
    if "hoa_textract_status" not in st.session_state:
        st.session_state["hoa_textract_status"] = None
    if "hoa_textract_pages" not in st.session_state:
        st.session_state["hoa_textract_pages"] = 0
    if "hoa_textract_timeout" not in st.session_state:
        st.session_state["hoa_textract_timeout"] = False
    if "hoa_textract_bucket" not in st.session_state:
        st.session_state["hoa_textract_bucket"] = None
    if "hoa_extraction_s3_key" not in st.session_state:
        st.session_state["hoa_extraction_s3_key"] = None
    if "hoa_last_analysis_mode" not in st.session_state:
        st.session_state["hoa_last_analysis_mode"] = "red"
    if "hoa_cost_by_document" not in st.session_state:
        st.session_state["hoa_cost_by_document"] = {}
    if "hoa_cost_breakdown" not in st.session_state:
        st.session_state["hoa_cost_breakdown"] = {}
    if "hoa_retain_document" not in st.session_state:
        st.session_state["hoa_retain_document"] = False
    if "hoa_retention_amount" not in st.session_state:
        st.session_state["hoa_retention_amount"] = 7
    if "hoa_retention_unit" not in st.session_state:
        st.session_state["hoa_retention_unit"] = "days"
    if "hoa_storage_class" not in st.session_state:
        st.session_state["hoa_storage_class"] = "Standard"
    if "hoa_extraction_autosave" not in st.session_state:
        st.session_state["hoa_extraction_autosave"] = True
    if "hoa_processing_submit" not in st.session_state:
        st.session_state["hoa_processing_submit"] = False
    if "hoa_processing_analysis" not in st.session_state:
        st.session_state["hoa_processing_analysis"] = False
    if "hoa_processing_use_previous" not in st.session_state:
        st.session_state["hoa_processing_use_previous"] = False
    if "hoa_processing_use_saved" not in st.session_state:
        st.session_state["hoa_processing_use_saved"] = False
    if "hoa_processing_followup" not in st.session_state:
        st.session_state["hoa_processing_followup"] = False
    if "hoa_last_analyzed_red_pages" not in st.session_state:
        st.session_state["hoa_last_analyzed_red_pages"] = None
    if "hoa_last_analyzed_green_pages" not in st.session_state:
        st.session_state["hoa_last_analyzed_green_pages"] = None


def _retention_hours(amount: int, unit: str) -> float:
    if unit == "weeks":
        return amount * HOURS_IN_WEEK
    if unit == "months":
        return amount * HOURS_IN_MONTH
    return amount * HOURS_IN_DAY


def _storage_rate_per_gb(storage_class: str) -> float:
    if storage_class == "Standard-IA":
        return S3_STORAGE_PER_GB_MONTH_STANDARD_IA
    if storage_class == "One Zone-IA":
        return S3_STORAGE_PER_GB_MONTH_ONE_ZONE_IA
    return S3_STORAGE_PER_GB_MONTH


def _estimate_storage_cost(
    size_bytes: int,
    retention_hours: float,
    storage_class: str,
) -> float:
    rate = _storage_rate_per_gb(storage_class)
    return (size_bytes / GB_IN_BYTES) * (retention_hours / HOURS_IN_MONTH) * rate


def _storage_rate_per_mb(storage_class: str) -> float:
    return _storage_rate_per_gb(storage_class) / 1024.0


def _format_currency(amount: float) -> str:
    return f"${amount:.2f}"


def _list_previous_uploads(bucket_name: str, prefix: str, owner_sub: str | None = None) -> List[dict]:
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    uploads = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not key or key.endswith("/"):
                continue
            name = key.split("/")[-1]
            if owner_sub and name.startswith(f"{owner_sub}-"):
                name = name[len(owner_sub) + 1 :]
            name = re.sub(r"^[0-9a-fA-F-]{8,}-", "", name)
            uploads.append(
                {
                    "key": key,
                    "name": name,
                    "size": obj.get("Size", 0),
                    "last_modified": obj.get("LastModified"),
                }
            )
    uploads.sort(key=lambda item: item.get("last_modified") or "", reverse=True)
    return uploads


def _sanitize_doc_name(document_name: str) -> str:
    if not document_name:
        return "unknown"
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "-", document_name.strip())
    sanitized = re.sub(r"-+", "-", sanitized).strip("-")
    return sanitized or "unknown"


def _save_extraction_to_s3(
    *,
    bucket_name: str,
    extraction: Any,
    source: str,
) -> str | None:
    if not bucket_name or not extraction:
        return None
    s3 = boto3.client("s3")
    doc_prefix = _sanitize_doc_name(extraction.document_name)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    key = f"hoa-extractions/{doc_prefix}/{timestamp}.json"
    payload = extraction_to_payload(extraction, source=source)
    s3.put_object(
        Bucket=bucket_name,
        Key=key,
        Body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json",
        Metadata={
            "page-count": str(len(extraction.pages)),
            "document-name": extraction.document_name or "",
        },
    )
    return key


def _list_saved_extractions(bucket_name: str, prefix: str = "hoa-extractions/") -> List[dict]:
    if not bucket_name:
        return []
    s3 = boto3.client("s3")
    paginator = s3.get_paginator("list_objects_v2")
    items = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not key or not key.endswith(".json"):
                continue
            parts = key.split("/")
            doc_part = parts[1] if len(parts) > 2 else "Document"
            stamp = parts[-1].replace(".json", "")
            last_modified = obj.get("LastModified")
            # Fetch metadata using head_object
            page_count = "?"
            try:
                head = s3.head_object(Bucket=bucket_name, Key=key)
                metadata = head.get("Metadata") or {}
                page_count = metadata.get("page-count", "?")
            except Exception:
                pass
            timestamp_label = (
                last_modified.strftime("%Y-%m-%d %H:%M")
                if last_modified
                else stamp
            )
            items.append(
                {
                    "key": key,
                    "label": f"{doc_part} ({timestamp_label}, {page_count} pages)",
                    "stamp": stamp,
                    "last_modified": last_modified,
                    "page_count": page_count,
                }
            )
    items.sort(key=lambda item: item.get("last_modified") or "", reverse=True)
    return items


def _load_extraction_from_s3(*, bucket_name: str, key: str) -> Any | None:
    if not bucket_name or not key:
        return None
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket=bucket_name, Key=key)
    payload = json.loads(obj["Body"].read())
    return payload_to_extraction(payload)


def _has_duplicate_upload(upload_name: str, uploads: List[dict]) -> bool:
    normalized = upload_name.strip().lower()
    return any(item.get("name", "").strip().lower() == normalized for item in uploads)


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


def _record_cost(
    input_tokens: int,
    output_tokens: int,
    document_name: str | None = None,
) -> float | None:
    profile_id = st.session_state.get("hoa_inference_profile")
    model_key = _get_pricing_key_for_profile(profile_id or "")
    registry = get_llm_pricing_registry()
    if not model_key or model_key not in registry:
        return None
    estimated_cost = estimate_request_cost(model_key, input_tokens, output_tokens)
    if document_name:
        by_doc = st.session_state.get("hoa_cost_by_document", {})
        by_doc[document_name] = by_doc.get(document_name, 0.0) + estimated_cost
        st.session_state["hoa_cost_by_document"] = by_doc
        _add_cost_component(document_name, "bedrock", estimated_cost)
    st.session_state["hoa_cost_records"].append(
        {
            "request_id": profile_id or "unknown",
            "model_id": model_key,
            "inference_profile": profile_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "estimated_cost_usd": round(estimated_cost, 6),
            "pricing_version": get_pricing_version(),
            "document_name": document_name,
        }
    )
    # Auto-save profile to persist costs
    auto_save_profile()
    return estimated_cost


def _render_profile_selector() -> str:
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
        default_profile_id = st.session_state.get("hoa_inference_profile")
        if not st.session_state.get("hoa_profile_initialized"):
            default_profile_id = next(iter(profile_options.keys()))
            st.session_state["hoa_inference_profile"] = default_profile_id
            st.session_state["hoa_profile_initialized"] = True
        elif default_profile_id not in profile_options:
            default_profile_id = next(iter(profile_options.keys()))

        selected_profile = st.selectbox(
            "Select Model",
            options=list(profile_options.keys()),
            format_func=lambda pid: profile_options[pid],
            index=list(profile_options.keys()).index(default_profile_id),
            key="hoa_inference_selector",
        )
        st.session_state["hoa_inference_profile"] = selected_profile
        return selected_profile


def _render_usage() -> None:
    total_input, total_output, total_cost = _recalculate_costs(
        st.session_state.get("hoa_cost_records", [])
    )
    st.caption(f"Usage: {total_input} in / {total_output} out · ${total_cost:.6f}")


def _add_cost_component(document_name: str, component: str, amount: float) -> None:
    breakdown = st.session_state.get("hoa_cost_breakdown", {})
    doc_breakdown = breakdown.get(document_name, {})
    doc_breakdown[component] = doc_breakdown.get(component, 0.0) + amount
    breakdown[document_name] = doc_breakdown
    st.session_state["hoa_cost_breakdown"] = breakdown


def _set_cost_component(document_name: str, component: str, amount: float) -> None:
    breakdown = st.session_state.get("hoa_cost_breakdown", {})
    doc_breakdown = breakdown.get(document_name, {})
    doc_breakdown[component] = amount
    breakdown[document_name] = doc_breakdown
    st.session_state["hoa_cost_breakdown"] = breakdown


def _get_document_total_cost(document_name: str) -> float:
    breakdown = st.session_state.get("hoa_cost_breakdown", {}).get(document_name, {})
    if breakdown:
        return sum(breakdown.values())
    return st.session_state.get("hoa_cost_by_document", {}).get(document_name, 0.0)


def _render_cost_status_bar() -> None:
    if not st.session_state.get("hoa_documents"):
        return
    doc_name = st.session_state.get("hoa_documents", [{}])[0].get("name")
    if not doc_name:
        return
    current = _get_document_total_cost(doc_name)
    st.markdown("#### Document Cost Status")
    st.caption(f"{doc_name} · Estimated total cost: ${current:.4f}")


def _render_cost_breakdown(document_name: str) -> None:
    breakdown = st.session_state.get("hoa_cost_breakdown", {}).get(document_name, {})
    if not breakdown:
        return
    st.markdown("#### Cost Breakdown")
    rows = []
    for component, amount in breakdown.items():
        rows.append({"Component": component.replace("_", " ").title(), "Cost (USD)": f"${amount:.4f}"})
    st.table(rows)


def _format_list_items(items: list[str]) -> str:
    if not items:
        return ""
    return "\n".join([f"- {item}" for item in items])


def _render_formatted_report(
    *,
    structured: dict[str, Any],
    document_name: str,
    analysis_type: str,
    overall_label: str,
    highlights_title: str,
    items: list[dict[str, Any]],
    balanced_considerations: list[str] | None = None,
) -> str:
    summary = structured.get("executive_summary", {})
    summary_text = summary.get("summary", "")
    overall_value = summary.get(overall_label, "—")
    community = document_name or "Uploaded HOA Document"
    items_section = []
    if items:
        items_section.append(f"## {highlights_title}\n")
        for idx, item in enumerate(items, start=1):
            title = item.get("title", "Highlight")
            category = item.get("category", "—").title()
            quoted = item.get("quoted_text", "")
            explanation = item.get("explanation", "")
            strength = item.get("strength") or item.get("severity") or "—"
            pages = ", ".join(str(p) for p in item.get("page_numbers", [])) or "—"
            items_section.append(
                "\n".join(
                    [
                        f"### {idx}. {category} · {title}",
                        f"**Support Level:** {str(strength).title()} · **Pages:** {pages}",
                        f"_Quote:_ {quoted}" if quoted else "",
                        explanation,
                        "",
                    ]
                )
            )

    balanced_text = _format_list_items(balanced_considerations or [])
    return "\n".join(
        [
            "# 🏡 HOA Homeowner Support Review",
            "",
            f"**Community:** {community}",
            f"**Analysis Type:** {analysis_type}",
            "**Source:** Uploaded HOA Governing Documents Only",
            "",
            "## 📊 Executive Overview",
            "",
            f"**{analysis_type} Level:** {str(overall_value).upper()}",
            "",
            summary_text,
            "",
            "---",
            "",
            *items_section,
            "",
            "## ⚖️ Balanced Considerations",
            "",
            balanced_text or "No additional considerations detected.",
            "",
            "_This analysis is informational only and not legal advice._",
        ]
    ).strip()


def _render_analysis_views(
    *,
    analysis: Any,
    is_green: bool,
    document_name: str,
) -> None:
    if not analysis:
        st.info("No analysis available yet.")
        return
    structured = analysis.structured or {}
    raw_markdown = analysis.markdown or ""
    raw_text = analysis.raw_text or ""
    if is_green:
        items = structured.get("benefits", [])
        report_md = _render_formatted_report(
            structured=structured,
            document_name=document_name,
            analysis_type="Homeowner Protection & Support Review",
            overall_label="overall_support",
            highlights_title="✅ Green Flag Highlights (Homeowner-Friendly Provisions)",
            items=items,
        )
    else:
        items = structured.get("flags", [])
        report_md = _render_formatted_report(
            structured=structured,
            document_name=document_name,
            analysis_type="Risk & Restriction Review",
            overall_label="overall_strictness",
            highlights_title="🚩 Red Flag Highlights (Potential Risks)",
            items=items,
        )

    report_html = f"<div style='white-space: pre-wrap; font-family: inherit;'>{report_md}</div>"
    report_tab, markdown_tab, json_tab, raw_tab, html_tab = st.tabs(
        ["Report", "Markdown", "JSON", "Raw Text", "HTML"]
    )
    with report_tab:
        st.markdown(report_md)
    with markdown_tab:
        st.text_area("Markdown", value=raw_markdown, height=420)
    with json_tab:
        if structured:
            st.json(structured)
        else:
            st.info("No structured JSON available for this analysis.")
    with raw_tab:
        st.text_area("Raw Text", value=raw_text or raw_markdown, height=420)
    with html_tab:
        st.markdown(report_html, unsafe_allow_html=True)


def render_document_vetting() -> None:
    _ensure_vetting_state()

    with st.expander("Document Vetting", expanded=False):
        with st.expander("Red Flag Analysis", expanded=False):
            st.markdown(
                """
                **Purpose**
                - This analysis is for risk discovery and decision support for prospective homeowners.
                - It only uses the uploaded HOA document text and does not add outside knowledge.

                **Objective**
                - Identify clauses that materially restrict homeowner rights, increase financial obligations,
                  limit resale or rental options, or allow broad/discretionary HOA enforcement.
                - Quote exact language, explain why it is a red flag, and categorize the risk
                  (financial, lifestyle, legal, resale).
                - **Overall strictness** summarizes how restrictive the document appears based on the
                  number and severity of red flags (low = few/mild, high = many/severe).

                **Red-Flag Categories and Checks**
                - **Financial exposure:** Special assessments, uncapped fee increases, fines, liens, or
                  foreclosure powers—cite sections and explain homeowner impact.
                - **Use & lifestyle restrictions:** Renting, home businesses, parking, vehicles, pets,
                  or architectural changes that could reasonably surprise a homeowner.
                - **Governance & enforcement risk:** Discretionary or unilateral enforcement powers,
                  limited appeal rights, or low-threshold amendment clauses.

                **Guardrails**
                - Routine HOA membership or standard dues are not flagged unless the text explicitly
                  indicates unusual financial risk (e.g., unlimited assessments, special assessments
                  without caps, unilateral fee changes, liens, or foreclosure powers).
                """
            )
        with st.expander("Green Flag Analysis", expanded=False):
            st.markdown(
                """
                **Purpose**
                - Highlight positive, homeowner-friendly provisions and protections.
                - Findings are based strictly on document text with no inferred benefits.

                **Objective**
                - Identify clauses favorable to homeowners that limit HOA discretion, cap financial exposure,
                  protect owner rights, or preserve flexibility for use, resale, or rental.
                - Quote exact language, explain why it is a positive feature, and categorize the benefit
                  (financial, lifestyle, governance, resale).
                - **Overall support** summarizes how homeowner-friendly the document appears based on the
                  number and strength of positive provisions (low = few/weak, high = many/strong).

                **Green-Flag Categories and Checks**
                - **Financial protections:** Limits on fee increases, caps on special assessments, notice
                  requirements, or protections against liens or fines.
                - **Use & flexibility:** Permissions for rentals, home offices, reasonable architectural changes,
                  pets, or parking with minimal approval burden.
                - **Governance & owner rights:** Owner votes for rule changes, appeal rights, due process,
                  or restrictions on arbitrary enforcement.

                **Safety / scope guardrail**
                - Base all findings strictly on the document text. Do not provide legal advice or assume
                  protections beyond what is stated.
                """
            )
        st.caption(
            "Upload HOA documents to flag restrictive clauses and ask follow-up questions. "
            "This analysis is informational only and not legal advice."
        )
        st.caption(
            "Large documents are analyzed in chunks and may take longer to complete."
        )

        _render_profile_selector()
        _render_usage()
        st.caption("Upload and analyze a document to see the cost breakdown below.")

        upload = st.file_uploader(
            "Upload HOA PDF",
            type=["pdf"],
            accept_multiple_files=False,
            key="hoa_document_upload",
        )
        if upload and upload.size > MAX_UPLOAD_BYTES:
            st.error(
                f"The selected PDF is {upload.size / (1024 * 1024):.1f} MB. "
                f"Please upload a file smaller than {MAX_UPLOAD_MB} MB."
            )
            upload = None

        retain_col, duration_col, unit_col = st.columns([1.3, 1, 1])
        with retain_col:
            retain_doc = st.checkbox(
                "Keep document after analysis",
                value=st.session_state.get("hoa_retain_document", False),
            )
        with duration_col:
            retention_amount = st.number_input(
                "Retention amount",
                min_value=1,
                max_value=365,
                value=st.session_state.get("hoa_retention_amount", 7),
                step=1,
            )
        with unit_col:
            retention_unit = st.selectbox(
                "Retention unit",
                options=["days", "weeks", "months"],
                index=["days", "weeks", "months"].index(
                    st.session_state.get("hoa_retention_unit", "days")
                ),
            )
        st.session_state["hoa_retain_document"] = retain_doc
        st.session_state["hoa_retention_amount"] = int(retention_amount)
        st.session_state["hoa_retention_unit"] = retention_unit

        storage_col = st.columns([1])[0]
        retention_hours = _retention_hours(
            st.session_state["hoa_retention_amount"],
            st.session_state["hoa_retention_unit"],
        )
        documents = st.session_state.get("hoa_documents", [])
        estimated_size = documents[0].get("size", 0) if documents else 0
        storage_options = ["Standard", "Standard-IA", "One Zone-IA"]
        storage_labels = {}
        for storage_class in storage_options:
            estimate = _estimate_storage_cost(estimated_size, retention_hours, storage_class)
            storage_labels[storage_class] = f"{storage_class} ({_format_currency(estimate)})"
        with storage_col:
            storage_class = st.selectbox(
                "Storage class",
                options=storage_options,
                format_func=lambda option: storage_labels[option],
                index=storage_options.index(st.session_state.get("hoa_storage_class", "Standard")),
            )
        st.session_state["hoa_storage_class"] = storage_class
        per_mb_rate = _storage_rate_per_mb(storage_class)
        st.caption(
            f"Estimated storage rate: ${per_mb_rate:.6f} per MB-month (prorated by retention duration)."
        )

        autosave_extraction = st.checkbox(
            "Auto-save extracted text",
            value=st.session_state.get("hoa_extraction_autosave", True),
            help="Store Textract text in S3 so you can reuse it without rerunning Textract.",
        )
        st.session_state["hoa_extraction_autosave"] = autosave_extraction

        owner_sub = get_owner_sub()
        bucket_prefix = get_storage_bucket_prefix()
        bucket_name = (
            bucket_name_for_owner(owner_sub, bucket_prefix)
            if owner_sub and bucket_prefix
            else None
        )
        previous_uploads: List[dict] = []
        selected_previous_key = None
        selected_previous_index = 0
        saved_extractions: List[dict] = []
        selected_extraction_key = None
        if bucket_name:
            previous_uploads = _list_previous_uploads(
                bucket_name,
                "hoa-uploads/",
                owner_sub=owner_sub,
            )
            saved_extractions = _list_saved_extractions(bucket_name)
            if upload and previous_uploads:
                for idx, item in enumerate(previous_uploads):
                    if item.get("name", "").strip().lower() == upload.name.strip().lower():
                        selected_previous_index = idx
                        break
            if previous_uploads:
                previous_labels = {
                    item["key"]: f"{item['name']} ({item['size'] / 1024:.1f} KB)"
                    for item in previous_uploads
                }
                selected_key = st.selectbox(
                    "Previous uploads",
                    options=[item["key"] for item in previous_uploads],
                    format_func=lambda key: previous_labels[key],
                    index=selected_previous_index,
                )
                selected_previous_key = selected_key
                delete_prev = st.button("Delete selected upload", key="hoa_delete_previous")
                if delete_prev and selected_key:
                    try:
                        s3 = boto3.client("s3")
                        s3.delete_object(Bucket=bucket_name, Key=selected_key)
                        st.success("Previous upload deleted.")
                    except Exception as exc:
                        st.error(f"Failed to delete upload: {exc}")
            if saved_extractions:
                extraction_labels = {
                    item["key"]: item["label"]
                    for item in saved_extractions
                }
                selected_extraction_key = st.selectbox(
                    "Saved extractions",
                    options=[item["key"] for item in saved_extractions],
                    format_func=lambda key: extraction_labels.get(key, key),
                    index=0,
                )
                selected_extraction = next(
                    (item for item in saved_extractions if item["key"] == selected_extraction_key),
                    None,
                )
                if selected_extraction:
                    st.caption(
                        f"Saved: {selected_extraction.get('label')} · Key: {selected_extraction.get('key')}"
                    )
                confirm_delete = st.checkbox(
                    "I understand this will permanently delete the saved extraction.",
                    value=False,
                    key="hoa_confirm_delete_saved_extraction",
                )
                delete_saved_extraction = st.button(
                    "Delete saved extraction",
                    key="hoa_delete_saved_extraction",
                    disabled=not selected_extraction_key or not confirm_delete,
                )
                if delete_saved_extraction and selected_extraction_key:
                    try:
                        s3 = boto3.client("s3")
                        s3.delete_object(Bucket=bucket_name, Key=selected_extraction_key)
                        st.success("Saved extraction deleted.")
                    except Exception as exc:
                        st.error(f"Failed to delete saved extraction: {exc}")

        duplicate_upload = False
        if upload and previous_uploads:
            # Skip duplicate check if we're already working with a document of the same name
            # (this happens after a successful upload when the page reruns)
            current_documents = st.session_state.get("hoa_documents", [])
            current_doc_name = current_documents[0].get("name") if current_documents else None
            already_working_with_file = (
                current_doc_name
                and current_doc_name.strip().lower() == upload.name.strip().lower()
                and st.session_state.get("hoa_extraction") is not None
            )
            if not already_working_with_file:
                duplicate_upload = _has_duplicate_upload(upload.name, previous_uploads)
                if duplicate_upload:
                    st.error(
                        "A document with this filename already exists in your uploads. "
                        "Please select it from Previous uploads and click 'Use Selected Upload', "
                        "or rename the file before uploading a new copy."
                    )

        if upload and not duplicate_upload:
            st.session_state["hoa_documents"] = [
                {
                    "name": upload.name,
                    "size": upload.size,
                }
            ]

        if st.session_state["hoa_documents"]:
            _render_cost_status_bar()
            doc_name = st.session_state.get("hoa_documents", [{}])[0].get("name")
            if doc_name:
                _render_cost_breakdown(doc_name)
                if st.session_state.get("hoa_retain_document"):
                    retention_label = f"{st.session_state.get('hoa_retention_amount')} {st.session_state.get('hoa_retention_unit')}"
                    storage_class = st.session_state.get("hoa_storage_class", "Standard")
                    st.caption(
                        f"Retention enabled · Storage cost reflects {retention_label} in {storage_class}."
                    )
            else:
                st.info("Cost breakdown will appear after analysis completes.")

        if st.session_state.get("hoa_retain_document") and st.session_state.get("hoa_textract_s3_key"):
            delete_now = st.button("Delete stored document now", key="hoa_delete_document")
            if delete_now:
                bucket_name = st.session_state.get("hoa_textract_bucket")
                s3_key = st.session_state.get("hoa_textract_s3_key")
                if bucket_name and s3_key:
                    s3 = boto3.client("s3")
                    try:
                        s3.delete_object(Bucket=bucket_name, Key=s3_key)
                        st.success("Stored document deleted.")
                        st.session_state["hoa_textract_s3_key"] = None
                        st.session_state["hoa_retain_document"] = False
                    except Exception as exc:
                        st.error(f"Failed to delete stored document: {exc}")

        is_processing_submit = st.session_state.get("hoa_processing_submit", False)
        is_processing_analysis = st.session_state.get("hoa_processing_analysis", False)
        is_processing_use_previous = st.session_state.get("hoa_processing_use_previous", False)
        is_processing_use_saved = st.session_state.get("hoa_processing_use_saved", False)
        is_processing_followup = st.session_state.get("hoa_processing_followup", False)
        
        # Check if this is a new file different from the currently processed one
        current_doc_name = st.session_state.get("hoa_documents", [{}])[0].get("name") if st.session_state.get("hoa_documents") else None
        has_extraction = st.session_state.get("hoa_extraction") is not None
        is_new_file = upload and (
            not current_doc_name 
            or upload.name.strip().lower() != current_doc_name.strip().lower()
            or not has_extraction
        )
        
        submit_button = st.button(
            "Submit",
            key="hoa_submit_upload",
            disabled=not is_new_file or duplicate_upload or is_processing_submit,
        )
        st.caption("Submit uploads the file and extracts text. Run analysis using the buttons below.")

        use_previous_button = st.button(
            "Use Selected Upload",
            key="hoa_use_previous",
            disabled=not selected_previous_key or is_processing_submit or is_processing_use_previous,
        )
        use_saved_extraction_button = st.button(
            "Use Saved Extraction",
            key="hoa_use_saved_extraction",
            disabled=not selected_extraction_key or is_processing_submit or is_processing_use_saved,
        )

        if submit_button and upload and not duplicate_upload:
            st.session_state["hoa_processing_submit"] = True
            owner_sub = get_owner_sub()
            bucket_prefix = get_storage_bucket_prefix()
            bucket_name = (
                bucket_name_for_owner(owner_sub, bucket_prefix)
                if owner_sub and bucket_prefix
                else None
            )
            if not bucket_name:
                st.error("Storage bucket is not configured. Set STORAGE_BUCKET_PREFIX and OwnerSub.")
                st.session_state["hoa_processing_submit"] = False
                return
            st.session_state["hoa_textract_bucket"] = bucket_name

            s3 = boto3.client("s3")
            status_placeholder = st.empty()
            try:
                s3.head_bucket(Bucket=bucket_name)
                status_placeholder.success("Bucket ready for use.")
            except Exception:
                status_placeholder.warning(
                    "Bucket does not exist yet. It will be created now."
                )
                try:
                    region = s3.meta.region_name or os.environ.get("AWS_REGION")
                    if region and region != "us-east-1":
                        s3.create_bucket(
                            Bucket=bucket_name,
                            CreateBucketConfiguration={"LocationConstraint": region},
                        )
                    else:
                        s3.create_bucket(Bucket=bucket_name)
                    status_placeholder.success("Bucket created and ready for use.")
                except Exception as exc:
                    status_placeholder.error(f"Failed to create bucket: {exc}")
                    st.session_state["hoa_processing_submit"] = False
                    return

            try:
                textract_status = st.empty()

                def _on_textract_progress(status: str, pages_found: int) -> None:
                    message = f"Textract status: {status}"
                    if pages_found:
                        message = f"Textract status: {status} · {pages_found} pages detected"
                    textract_status.info(message)

                with st.spinner("Running Textract..."):
                    job_id, s3_key = start_textract_job(
                        upload.getvalue(),
                        upload.name,
                        bucket_name=bucket_name,
                    )
                    st.session_state["hoa_textract_job_id"] = job_id
                    st.session_state["hoa_textract_s3_key"] = s3_key
                    blocks = poll_textract_job(
                        job_id,
                        on_progress=_on_textract_progress,
                        poll_delay_seconds=2.0,
                        max_polls=180,
                    )

                if not st.session_state.get("hoa_retain_document"):
                    cleanup_textract_job(s3_key, bucket_name=bucket_name)
                    st.session_state["hoa_textract_s3_key"] = None

                st.session_state["hoa_textract_job_id"] = None
                st.session_state["hoa_textract_status"] = None

                extraction = blocks_to_extraction(blocks, upload.name)
                st.session_state["hoa_extraction"] = extraction
                st.session_state["hoa_analysis"] = None
                st.session_state["hoa_green_analysis"] = None
                st.session_state["hoa_last_analyzed_red_pages"] = None
                st.session_state["hoa_last_analyzed_green_pages"] = None

                if st.session_state.get("hoa_extraction_autosave", True):
                    st.session_state["hoa_extraction_s3_key"] = _save_extraction_to_s3(
                        bucket_name=bucket_name,
                        extraction=extraction,
                        source="textract",
                    )

                page_count = len(extraction.pages)
                doc_size = st.session_state.get("hoa_documents", [{}])[0].get("size", 0)

                if st.session_state.get("hoa_retain_document"):
                    retention_hours = _retention_hours(
                        st.session_state.get("hoa_retention_amount", 7),
                        st.session_state.get("hoa_retention_unit", "days"),
                    )
                else:
                    retention_hours = S3_STAGING_HOURS

                storage_cost = _estimate_storage_cost(
                    doc_size,
                    retention_hours,
                    st.session_state.get("hoa_storage_class", "Standard"),
                )
                request_cost = ((2 * S3_PUT_REQUEST_PER_1000) + (2 * S3_GET_REQUEST_PER_1000)) / 1000.0
                textract_cost = page_count * TEXTRACT_TEXT_DETECTION_PER_PAGE

                _set_cost_component(extraction.document_name, "s3_storage", storage_cost)
                _set_cost_component(extraction.document_name, "s3_requests", request_cost)
                _set_cost_component(extraction.document_name, "textract", textract_cost)

                record_document_operation(
                    operation_type="textract",
                    document_name=extraction.document_name,
                    cost_usd=textract_cost,
                    metadata={"pages": page_count},
                )
                record_document_operation(
                    operation_type="s3_storage",
                    document_name=extraction.document_name,
                    cost_usd=storage_cost,
                    metadata={
                        "size_bytes": doc_size,
                        "retention_hours": retention_hours,
                        "storage_class": st.session_state.get("hoa_storage_class", "Standard"),
                    },
                )
                record_document_operation(
                    operation_type="s3_requests",
                    document_name=extraction.document_name,
                    cost_usd=request_cost,
                    metadata={"operations": "upload+download"},
                )

                st.success(f"Extraction complete! {page_count} pages extracted.")
                st.session_state["hoa_processing_submit"] = False
            except Exception as exc:
                st.session_state["hoa_processing_submit"] = False
                st.error(f"Failed to start Textract: {exc}")

        if use_previous_button and selected_previous_key:
            st.session_state["hoa_processing_use_previous"] = True
            if not bucket_name:
                st.error("Storage bucket is not configured. Set STORAGE_BUCKET_PREFIX and OwnerSub.")
                st.session_state["hoa_processing_use_previous"] = False
                return
            st.session_state["hoa_textract_bucket"] = bucket_name
            st.session_state["hoa_textract_s3_key"] = selected_previous_key

            matching = next(
                (item for item in previous_uploads if item.get("key") == selected_previous_key),
                None,
            )
            if matching:
                st.session_state["hoa_documents"] = [
                    {
                        "name": matching.get("name", "Uploaded document"),
                        "size": matching.get("size", 0),
                    }
                ]

            try:
                textract_status = st.empty()

                def _on_textract_progress(status: str, pages_found: int) -> None:
                    message = f"Textract status: {status}"
                    if pages_found:
                        message = f"Textract status: {status} · {pages_found} pages detected"
                    textract_status.info(message)

                with st.spinner("Running Textract on selected upload..."):
                    job_id = start_textract_job_for_s3_key(
                        selected_previous_key,
                        bucket_name=bucket_name,
                    )
                    st.session_state["hoa_textract_job_id"] = job_id
                    blocks = poll_textract_job(
                        job_id,
                        on_progress=_on_textract_progress,
                        poll_delay_seconds=2.0,
                        max_polls=180,
                    )

                if not st.session_state.get("hoa_retain_document"):
                    cleanup_textract_job(selected_previous_key, bucket_name=bucket_name)
                    st.session_state["hoa_textract_s3_key"] = None

                st.session_state["hoa_textract_job_id"] = None
                st.session_state["hoa_textract_status"] = None

                doc_name = st.session_state.get("hoa_documents", [{}])[0].get("name", "document")
                extraction = blocks_to_extraction(blocks, doc_name)
                st.session_state["hoa_extraction"] = extraction
                st.session_state["hoa_analysis"] = None
                st.session_state["hoa_green_analysis"] = None
                st.session_state["hoa_last_analyzed_red_pages"] = None
                st.session_state["hoa_last_analyzed_green_pages"] = None

                if st.session_state.get("hoa_extraction_autosave", True):
                    st.session_state["hoa_extraction_s3_key"] = _save_extraction_to_s3(
                        bucket_name=bucket_name,
                        extraction=extraction,
                        source="textract",
                    )

                page_count = len(extraction.pages)
                doc_size = st.session_state.get("hoa_documents", [{}])[0].get("size", 0)

                if st.session_state.get("hoa_retain_document"):
                    retention_hours = _retention_hours(
                        st.session_state.get("hoa_retention_amount", 7),
                        st.session_state.get("hoa_retention_unit", "days"),
                    )
                else:
                    retention_hours = S3_STAGING_HOURS

                storage_cost = _estimate_storage_cost(
                    doc_size,
                    retention_hours,
                    st.session_state.get("hoa_storage_class", "Standard"),
                )
                request_cost = ((2 * S3_PUT_REQUEST_PER_1000) + (2 * S3_GET_REQUEST_PER_1000)) / 1000.0
                textract_cost = page_count * TEXTRACT_TEXT_DETECTION_PER_PAGE

                _set_cost_component(extraction.document_name, "s3_storage", storage_cost)
                _set_cost_component(extraction.document_name, "s3_requests", request_cost)
                _set_cost_component(extraction.document_name, "textract", textract_cost)

                record_document_operation(
                    operation_type="textract",
                    document_name=extraction.document_name,
                    cost_usd=textract_cost,
                    metadata={"pages": page_count},
                )
                record_document_operation(
                    operation_type="s3_storage",
                    document_name=extraction.document_name,
                    cost_usd=storage_cost,
                    metadata={
                        "size_bytes": doc_size,
                        "retention_hours": retention_hours,
                        "storage_class": st.session_state.get("hoa_storage_class", "Standard"),
                    },
                )
                record_document_operation(
                    operation_type="s3_requests",
                    document_name=extraction.document_name,
                    cost_usd=request_cost,
                    metadata={"operations": "upload+download"},
                )

                st.success(f"Extraction complete! {page_count} pages extracted.")
                st.session_state["hoa_processing_use_previous"] = False
            except Exception as exc:
                st.session_state["hoa_processing_use_previous"] = False
                st.error(f"Failed to start Textract: {exc}")

        if use_saved_extraction_button and selected_extraction_key:
            if not bucket_name:
                st.error("Storage bucket is not configured. Set STORAGE_BUCKET_PREFIX and OwnerSub.")
                st.session_state["hoa_processing_use_saved"] = False
            else:
                try:
                    st.session_state["hoa_processing_use_saved"] = True
                    stored = _load_extraction_from_s3(
                        bucket_name=bucket_name,
                        key=selected_extraction_key,
                    )
                    if stored:
                        st.session_state["hoa_extraction"] = stored
                        st.session_state["hoa_analysis"] = None
                        st.session_state["hoa_green_analysis"] = None
                        st.session_state["hoa_last_analyzed_red_pages"] = None
                        st.session_state["hoa_last_analyzed_green_pages"] = None
                        st.session_state["hoa_documents"] = [
                            {
                                "name": stored.document_name,
                                "size": 0,
                            }
                        ]
                        st.session_state["hoa_extraction_s3_key"] = selected_extraction_key
                        st.success(
                            f"Loaded saved extraction: {stored.document_name} ({stored.page_count} pages)"
                        )
                        st.session_state["hoa_processing_use_saved"] = False
                except Exception as exc:
                    st.session_state["hoa_processing_use_saved"] = False
                    st.error(f"Failed to load saved extraction: {exc}")

        extraction_ready = st.session_state.get("hoa_extraction") is not None
        
        # Get current page range for analysis (will be set later in the UI, use defaults for now)
        current_page_start = st.session_state.get("hoa_page_start", 1)
        extraction = st.session_state.get("hoa_extraction")
        default_end = len(extraction.pages) if extraction else 1
        current_page_end = st.session_state.get("hoa_page_end", default_end)
        current_page_range = (current_page_start, current_page_end)
        
        # Check if page range changed since last analysis
        last_red_pages = st.session_state.get("hoa_last_analyzed_red_pages")
        last_green_pages = st.session_state.get("hoa_last_analyzed_green_pages")
        
        # Red analysis enabled if: extraction ready, not processing, and either never run or page range changed
        red_page_range_changed = last_red_pages is None or last_red_pages != current_page_range
        green_page_range_changed = last_green_pages is None or last_green_pages != current_page_range
        
        analyze_button = st.button(
            "Run Red-Flag Analysis",
            key="hoa_analyze",
            disabled=not extraction_ready or is_processing_analysis or not red_page_range_changed,
        )
        green_button = st.button(
            "Run Green-Flag Analysis",
            key="hoa_green_analyze",
            disabled=not extraction_ready or is_processing_analysis or not green_page_range_changed,
        )

        # Page range selection (shown when extraction is ready)
        if extraction_ready:
            extraction_for_pages = st.session_state["hoa_extraction"]
            total_pages = len(extraction_for_pages.pages)
            st.markdown("#### Page Selection")
            range_col1, range_col2 = st.columns(2)
            with range_col1:
                range_start = st.number_input(
                    "Start page",
                    min_value=1,
                    max_value=max(1, total_pages),
                    value=st.session_state.get("hoa_page_start", 1),
                    step=1,
                    key="hoa_page_start",
                )
            with range_col2:
                range_end = st.number_input(
                    "End page",
                    min_value=1,
                    max_value=max(1, total_pages),
                    value=st.session_state.get("hoa_page_end", total_pages),
                    step=1,
                    key="hoa_page_end",
                )
            if range_start > range_end:
                st.warning("Start page cannot be greater than end page.")

        if analyze_button or green_button:
            if not st.session_state.get("hoa_extraction"):
                st.warning("Upload and submit a document before running analysis.")
            else:
                st.session_state["hoa_processing_analysis"] = True
                extraction = st.session_state.get("hoa_extraction")
                context_text = build_page_context(extraction)
                doc_name = extraction.document_name
                model_id = st.session_state.get("hoa_inference_profile")
                analysis_status = st.empty()

                def _on_analysis_progress(current: int, total: int) -> None:
                    analysis_status.info(f"Analyzing chunk {current} of {total}...")

                # Get current page range for tracking
                analysis_page_start = st.session_state.get("hoa_page_start", 1)
                analysis_page_end = st.session_state.get("hoa_page_end", len(extraction.pages))
                analysis_page_range = (analysis_page_start, analysis_page_end)
                
                if analyze_button:
                    with st.spinner("Running red-flag analysis..."):
                        analysis = analyze_document_chunked(
                            [page.text for page in extraction.pages],
                            model_id=model_id,
                            on_progress=_on_analysis_progress,
                        )
                    st.session_state["hoa_last_analysis_mode"] = "red"
                    st.session_state["hoa_analysis"] = analysis
                    st.session_state["hoa_last_analyzed_red_pages"] = analysis_page_range
                else:
                    with st.spinner("Running green-flag analysis..."):
                        analysis = analyze_document_chunked_green(
                            [page.text for page in extraction.pages],
                            model_id=model_id,
                            on_progress=_on_analysis_progress,
                        )
                    st.session_state["hoa_last_analysis_mode"] = "green"
                    st.session_state["hoa_green_analysis"] = analysis
                    st.session_state["hoa_last_analyzed_green_pages"] = analysis_page_range
                analysis_status.success("Analysis complete.")
                chunk_count = max(1, (len(extraction.pages) + 11) // 12)
                input_tokens = analysis.input_tokens
                output_tokens = analysis.output_tokens
                if not input_tokens or not output_tokens:
                    input_tokens = max(1, len(context_text.split()) * chunk_count)
                    output_tokens = max(40, len(analysis.markdown.split()) * chunk_count)
                _record_cost(
                    input_tokens,
                    output_tokens,
                    document_name=doc_name,
                )
                st.session_state["hoa_processing_analysis"] = False
        analysis = st.session_state.get("hoa_analysis")
        green_analysis = st.session_state.get("hoa_green_analysis")
        with st.expander("Red/Green Flag Results", expanded=True):
            if analysis or green_analysis:
                if analysis and green_analysis:
                    red_tab, green_tab = st.tabs(["Red Flags", "Green Flags"])
                    with red_tab:
                        _render_analysis_views(
                            analysis=analysis,
                            is_green=False,
                            document_name=doc_name,
                        )

                    with green_tab:
                        _render_analysis_views(
                            analysis=green_analysis,
                            is_green=True,
                            document_name=doc_name,
                        )
                elif analysis:
                    _render_analysis_views(
                        analysis=analysis,
                        is_green=False,
                        document_name=doc_name,
                    )
                else:
                    _render_analysis_views(
                        analysis=green_analysis,
                        is_green=True,
                        document_name=doc_name,
                    )
            else:
                st.info("Run a red-flag or green-flag analysis to see results here.")

        if st.session_state.get("hoa_extraction"):
            extraction = st.session_state["hoa_extraction"]
            total_pages = len(extraction.pages)
            st.markdown("#### Follow-Up Questions")
            st.caption("Follow-up answers are limited to the extracted text in the selected page range.")

            default_start = st.session_state.get("hoa_page_start", 1)
            default_end = st.session_state.get("hoa_page_end", total_pages)

            with st.form("hoa_followup_form", clear_on_submit=True):
                question = st.text_input(
                    "Question",
                    placeholder="e.g., Are rentals allowed?",
                )
                answer_type = st.selectbox(
                    "Answer type",
                    options=["Summarized", "Yes/No + cite", "Clause lookup"],
                    index=0,
                    help="Used to shape the response; citations are always included when found.",
                )
                range_col1, range_col2 = st.columns(2)
                with range_col1:
                    followup_start = st.number_input(
                        "Start page (optional)",
                        min_value=1,
                        max_value=max(1, total_pages),
                        value=default_start,
                        step=1,
                    )
                with range_col2:
                    followup_end = st.number_input(
                        "End page (optional)",
                        min_value=1,
                        max_value=max(1, total_pages),
                        value=default_end,
                        step=1,
                    )
                submit_followup = st.form_submit_button(
                    "Ask question",
                    disabled=is_processing_followup,
                )

            if submit_followup:
                st.session_state["hoa_processing_followup"] = True
                if not question.strip():
                    st.warning("Please enter a follow-up question.")
                    st.session_state["hoa_processing_followup"] = False
                    return
                if followup_start > followup_end:
                    st.error("Start page cannot be greater than end page.")
                    st.session_state["hoa_processing_followup"] = False
                    return

                filtered_pages = [
                    page
                    for page in extraction.pages
                    if followup_start <= page.page_number <= followup_end
                ]
                if not filtered_pages:
                    st.error("No extracted pages found in that range.")
                    st.session_state["hoa_processing_followup"] = False
                    return

                context_text = build_page_context(
                    DocumentExtraction(
                        document_name=extraction.document_name,
                        pages=filtered_pages,
                    )
                )
                model_id = st.session_state.get("hoa_inference_profile")
                shaped_question = f"Answer type: {answer_type}. {question.strip()}"
                followup_status = st.empty()

                def _on_followup_progress(current: int, total: int) -> None:
                    followup_status.info(
                        f"Searching extracted text... (chunk {current} of {total})"
                    )

                response = answer_question_chunked(
                    [(page.page_number, page.text) for page in filtered_pages],
                    shaped_question,
                    document_name=extraction.document_name,
                    model_id=model_id,
                    on_progress=_on_followup_progress,
                )
                followup_status.success("Follow-up answer ready.")
                input_tokens = response.get("input_tokens")
                output_tokens = response.get("output_tokens")
                if not input_tokens or not output_tokens:
                    input_tokens = max(1, len(shaped_question.split()))
                    output_tokens = max(20, len(str(response).split()))
                _record_cost(
                    int(input_tokens),
                    int(output_tokens),
                    document_name=extraction.document_name,
                )

                query = VettingQuery(
                    question=question.strip(),
                    answer=response.get("answer", ""),
                    document_name=extraction.document_name,
                    page_numbers=response.get("page_numbers", []),
                    quoted_text=response.get("quoted_text", ""),
                    confidence=response.get("confidence", "low"),
                    not_found=bool(response.get("not_found")),
                    answer_type=answer_type,
                    page_range=(int(followup_start), int(followup_end)),
                )
                st.session_state["hoa_queries"].append(query)
                st.session_state["hoa_processing_followup"] = False

        if st.session_state.get("hoa_queries"):
            st.markdown("#### Query History")
            for idx, query in enumerate(reversed(st.session_state["hoa_queries"]), start=1):
                with st.expander(f"Question {idx}: {query.question}", expanded=False):
                    pages = ", ".join(str(p) for p in query.page_numbers) or "—"
                    page_range = query.page_range or ("—", "—")
                    not_found_label = "Yes" if query.not_found else "No"
                    st.markdown(
                        f"**Answer type:** {query.answer_type}\n\n"
                        f"**Answer:**\n{query.answer}\n\n"
                        f"**Document:** {query.document_name}\n\n"
                        f"**Page range used:** {page_range[0]}–{page_range[1]}\n\n"
                        f"**Pages cited:** {pages}\n\n"
                        f"**Confidence:** {query.confidence.title()} · **Not found:** {not_found_label}\n\n"
                        f"**Quoted text:** _{query.quoted_text or '—'}_"
                    )


def _render_analysis_summary(structured: dict) -> None:
    summary = structured.get("executive_summary", {})
    flags = structured.get("flags", [])
    strictness = summary.get("overall_strictness", "—")
    summary_text = summary.get("summary", "")

    html_parts = [
        "<div style='max-height: 480px; overflow-y: auto; padding: 12px; border: 1px solid #E0E0E0; border-radius: 8px; background-color: #FFF4F4;'>",
        "<div style='font-size: 0.95rem; font-weight: 600; margin-bottom: 8px;'>Executive Overview</div>",
        f"<div style='margin-bottom: 6px;'><strong>Overall strictness:</strong> {strictness}</div>",
        f"<div style='margin-bottom: 14px; line-height: 1.45;'>{summary_text}</div>",
        "<div style='font-size: 0.95rem; font-weight: 600; margin: 14px 0 8px;'>Flagged Clauses</div>",
    ]

    if not flags:
        html_parts.append("<div>No flags detected.</div>")
    else:
        for flag in flags:
            title = flag.get("title", "Flag")
            category = flag.get("category", "—")
            severity = flag.get("severity", "—")
            confidence = flag.get("confidence", "—")
            pages = ", ".join(str(p) for p in flag.get("page_numbers", [])) or "—"
            quoted = flag.get("quoted_text", "")
            explanation = flag.get("explanation", "")
            html_parts.append(
                "<div style='padding: 10px 12px; border: 1px solid #E6E6E6; border-radius: 8px; margin-bottom: 10px; background: #FFFFFF;'>"
                f"<div style='font-weight: 600; margin-bottom: 4px;'>{title}</div>"
                f"<div style='font-size: 0.85rem; color: #444; margin-bottom: 6px;'>"
                f"{category} · {severity} · {confidence} · pages {pages}</div>"
                f"<div style='font-style: italic; margin-bottom: 6px; color: #333;'>{quoted}</div>"
                f"<div style='color: #333; line-height: 1.45;'>{explanation}</div>"
                "</div>"
            )

    html_parts.append("</div>")
    st.markdown("\n".join(html_parts), unsafe_allow_html=True)


def _render_green_analysis_summary(structured: dict) -> None:
    summary = structured.get("executive_summary", {})
    benefits = structured.get("benefits", [])
    support = summary.get("overall_support", "—")
    summary_text = summary.get("summary", "")

    html_parts = [
        "<div style='max-height: 480px; overflow-y: auto; padding: 12px; border: 1px solid #E0E0E0; border-radius: 8px; background-color: #F4FFF7;'>",
        "<div style='font-size: 0.95rem; font-weight: 600; margin-bottom: 8px;'>Executive Overview</div>",
        f"<div style='margin-bottom: 6px;'><strong>Overall support:</strong> {support}</div>",
        f"<div style='margin-bottom: 14px; line-height: 1.45;'>{summary_text}</div>",
        "<div style='font-size: 0.95rem; font-weight: 600; margin: 14px 0 8px;'>Green Flag Highlights</div>",
    ]

    if not benefits:
        html_parts.append("<div>No green flags detected.</div>")
    else:
        for benefit in benefits:
            title = benefit.get("title", "Benefit")
            category = benefit.get("category", "—")
            strength = benefit.get("strength", "—")
            confidence = benefit.get("confidence", "—")
            pages = ", ".join(str(p) for p in benefit.get("page_numbers", [])) or "—"
            quoted = benefit.get("quoted_text", "")
            explanation = benefit.get("explanation", "")
            html_parts.append(
                "<div style='padding: 10px 12px; border: 1px solid #E6E6E6; border-radius: 8px; margin-bottom: 10px; background: #FFFFFF;'>"
                f"<div style='font-weight: 600; margin-bottom: 4px;'>{title}</div>"
                f"<div style='font-size: 0.85rem; color: #444; margin-bottom: 6px;'>"
                f"{category} · {strength} · {confidence} · pages {pages}</div>"
                f"<div style='font-style: italic; margin-bottom: 6px; color: #333;'>{quoted}</div>"
                f"<div style='color: #333; line-height: 1.45;'>{explanation}</div>"
                "</div>"
            )

    html_parts.append("</div>")
    st.markdown("\n".join(html_parts), unsafe_allow_html=True)
