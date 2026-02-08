"""Streamlit UI for HOA document vetting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List
import os
import re

import streamlit as st

from config.pricing import (
    CLAUDE_INFERENCE_PROFILES,
    PRICING_REGISTRY,
    PRICING_VERSION,
    estimate_request_cost,
)
from hoa.analysis import analyze_document_chunked, analyze_document_chunked_green, answer_question
from hoa.extraction import (
    build_page_context,
    start_textract_job,
    start_textract_job_for_s3_key,
    poll_textract_job,
    cleanup_textract_job,
    blocks_to_extraction,
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


@dataclass
class VettingQuery:
    question: str
    answer: str
    document_name: str
    page_numbers: List[int]
    quoted_text: str


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
    if "hoa_processing_submit" not in st.session_state:
        st.session_state["hoa_processing_submit"] = False
    if "hoa_processing_analysis" not in st.session_state:
        st.session_state["hoa_processing_analysis"] = False
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
    if not model_key or model_key not in PRICING_REGISTRY:
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
            "pricing_version": PRICING_VERSION,
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
    total_input = sum(
        record.get("input_tokens", 0)
        for record in st.session_state.get("hoa_cost_records", [])
    )
    total_output = sum(
        record.get("output_tokens", 0)
        for record in st.session_state.get("hoa_cost_records", [])
    )
    total_cost = sum(
        record.get("estimated_cost_usd", 0.0)
        for record in st.session_state.get("hoa_cost_records", [])
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
        if bucket_name:
            previous_uploads = _list_previous_uploads(
                bucket_name,
                "hoa-uploads/",
                owner_sub=owner_sub,
            )
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
                        st.rerun()
                    except Exception as exc:
                        st.error(f"Failed to delete upload: {exc}")

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
            disabled=not selected_previous_key or is_processing_submit,
        )

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
        retry_button = st.button(
            "Retry Textract Polling",
            key="hoa_textract_retry",
            disabled=not st.session_state.get("hoa_textract_timeout"),
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
                    return

            try:
                with st.spinner("Uploading document to Textract..."):
                    job_id, s3_key = start_textract_job(
                        upload.getvalue(),
                        upload.name,
                        bucket_name=bucket_name,
                    )
                st.session_state["hoa_textract_job_id"] = job_id
                st.session_state["hoa_textract_s3_key"] = s3_key
                st.session_state["hoa_textract_timeout"] = False
                st.session_state["hoa_textract_pages"] = 0

                status_placeholder = st.empty()
                progress_placeholder = st.empty()

                def _on_progress(status: str, pages: int) -> None:
                    st.session_state["hoa_textract_status"] = status
                    st.session_state["hoa_textract_pages"] = pages
                    status_placeholder.info(f"Textract status: {status}")
                    progress_placeholder.caption(
                        f"Pages detected so far: {pages}"
                    )

                with st.spinner("Extracting text with Textract..."):
                    blocks = poll_textract_job(
                        job_id,
                        poll_delay_seconds=2.0,
                        max_polls=120,
                        on_progress=_on_progress,
                    )
                if not st.session_state.get("hoa_retain_document"):
                    cleanup_textract_job(s3_key, bucket_name=bucket_name)
                    st.session_state["hoa_textract_s3_key"] = None
                st.session_state["hoa_textract_job_id"] = None

                extraction = blocks_to_extraction(blocks, upload.name)
                st.session_state["hoa_extraction"] = extraction
                page_count = len(extraction.pages)
                st.success(f"Document type: PDF · Pages detected: {page_count}")
                context_text = build_page_context(extraction)
                doc_name = extraction.document_name
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
                _set_cost_component(doc_name, "s3_storage", storage_cost)
                _set_cost_component(doc_name, "s3_requests", request_cost)
                _set_cost_component(doc_name, "textract", textract_cost)

                st.session_state["hoa_processing_submit"] = False
                st.rerun()
            except TimeoutError:
                st.session_state["hoa_textract_timeout"] = True
                st.session_state["hoa_processing_submit"] = False
                st.warning(
                    "Textract is still processing. Click 'Retry Textract Polling' to continue without re-uploading."
                )
            except Exception as exc:
                st.session_state["hoa_processing_submit"] = False
                st.error(f"Textract failed: {exc}")

        if use_previous_button and selected_previous_key:
            st.session_state["hoa_processing_submit"] = True
            if not bucket_name:
                st.error("Storage bucket is not configured. Set STORAGE_BUCKET_PREFIX and OwnerSub.")
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

            status_placeholder = st.empty()
            progress_placeholder = st.empty()
            try:
                with st.spinner("Starting Textract on selected upload..."):
                    job_id = start_textract_job_for_s3_key(
                        selected_previous_key,
                        bucket_name=bucket_name,
                    )
                st.session_state["hoa_textract_job_id"] = job_id
                st.session_state["hoa_textract_timeout"] = False
                st.session_state["hoa_textract_pages"] = 0

                def _on_progress(status: str, pages: int) -> None:
                    st.session_state["hoa_textract_status"] = status
                    st.session_state["hoa_textract_pages"] = pages
                    status_placeholder.info(f"Textract status: {status}")
                    progress_placeholder.caption(
                        f"Pages detected so far: {pages}"
                    )

                with st.spinner("Extracting text with Textract..."):
                    blocks = poll_textract_job(
                        job_id,
                        poll_delay_seconds=2.0,
                        max_polls=120,
                        on_progress=_on_progress,
                    )
                st.session_state["hoa_textract_job_id"] = None
                st.session_state["hoa_textract_timeout"] = False

                extraction = blocks_to_extraction(
                    blocks,
                    matching.get("name", "Uploaded document") if matching else "Uploaded document",
                )
                st.session_state["hoa_extraction"] = extraction
                page_count = len(extraction.pages)
                st.success(f"Document type: PDF · Pages detected: {page_count}")

                doc_name = extraction.document_name
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
                _set_cost_component(doc_name, "s3_storage", storage_cost)
                _set_cost_component(doc_name, "s3_requests", request_cost)
                _set_cost_component(doc_name, "textract", textract_cost)

                st.rerun()
            except TimeoutError:
                st.session_state["hoa_textract_timeout"] = True
                st.warning(
                    "Textract is still processing. Click 'Retry Textract Polling' to continue without re-uploading."
                )
            except Exception as exc:
                st.error(f"Textract failed: {exc}")

        if (analyze_button or green_button) and not st.session_state.get("hoa_extraction"):
            st.warning("Upload and submit a document before running analysis.")
            return

        if (analyze_button or green_button) and st.session_state.get("hoa_extraction"):
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
            st.rerun()

        if retry_button:
            job_id = st.session_state.get("hoa_textract_job_id")
            s3_key = st.session_state.get("hoa_textract_s3_key")
            bucket_name = st.session_state.get("hoa_textract_bucket")
            if not job_id or not s3_key:
                st.error("No Textract job to resume.")
            else:
                status_placeholder = st.empty()
                progress_placeholder = st.empty()

                def _on_progress(status: str, pages: int) -> None:
                    st.session_state["hoa_textract_status"] = status
                    st.session_state["hoa_textract_pages"] = pages
                    status_placeholder.info(f"Textract status: {status}")
                    progress_placeholder.caption(
                        f"Pages detected so far: {pages}"
                    )

                try:
                    with st.spinner("Resuming Textract polling..."):
                        blocks = poll_textract_job(
                            job_id,
                            poll_delay_seconds=2.0,
                            max_polls=120,
                            on_progress=_on_progress,
                        )
                    if not st.session_state.get("hoa_retain_document"):
                        cleanup_textract_job(s3_key, bucket_name=bucket_name)
                        st.session_state["hoa_textract_s3_key"] = None
                    st.session_state["hoa_textract_job_id"] = None
                    st.session_state["hoa_textract_bucket"] = None
                    st.session_state["hoa_textract_timeout"] = False

                    extraction = blocks_to_extraction(blocks, upload.name)
                    st.session_state["hoa_extraction"] = extraction
                    page_count = len(extraction.pages)
                    st.success(f"Document type: PDF · Pages detected: {page_count}")
                    context_text = build_page_context(extraction)
                    doc_name = extraction.document_name
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
                    _set_cost_component(doc_name, "s3_storage", storage_cost)
                    _set_cost_component(doc_name, "s3_requests", request_cost)
                    _set_cost_component(doc_name, "textract", textract_cost)

                    st.rerun()
                except TimeoutError:
                    st.session_state["hoa_textract_timeout"] = True
                    st.warning(
                        "Textract is still processing. Click 'Retry Textract Polling' to continue without re-uploading."
                    )
                except Exception as exc:
                    st.error(f"Textract failed: {exc}")

        analysis = st.session_state.get("hoa_analysis")
        green_analysis = st.session_state.get("hoa_green_analysis")
        if analysis or green_analysis:
            if analysis and green_analysis:
                red_tab, green_tab = st.tabs(["Red Flags", "Green Flags"])
                with red_tab:
                    st.markdown("#### Red-Flag Analysis Summary")
                    if analysis.structured:
                        _render_analysis_summary(analysis.structured)
                    else:
                        st.text_area(
                            "Red-Flag Analysis Summary",
                            value=analysis.markdown,
                            height=420,
                            label_visibility="collapsed",
                        )
                    with st.expander("Red-Flag Structured JSON", expanded=False):
                        if analysis.structured:
                            st.json(analysis.structured)
                        else:
                            st.info("No structured JSON was returned. Showing raw output below.")
                            st.code(analysis.markdown, language="markdown")

                with green_tab:
                    st.markdown("#### Green-Flag Analysis Summary")
                    if green_analysis.structured:
                        _render_green_analysis_summary(green_analysis.structured)
                    else:
                        st.text_area(
                            "Green-Flag Analysis Summary",
                            value=green_analysis.markdown,
                            height=420,
                            label_visibility="collapsed",
                        )
                    with st.expander("Green-Flag Structured JSON", expanded=False):
                        if green_analysis.structured:
                            st.json(green_analysis.structured)
                        else:
                            st.info("No structured JSON was returned. Showing raw output below.")
                            st.code(green_analysis.markdown, language="markdown")
            elif analysis:
                st.markdown("#### Red-Flag Analysis Summary")
                if analysis.structured:
                    _render_analysis_summary(analysis.structured)
                else:
                    st.text_area(
                        "Red-Flag Analysis Summary",
                        value=analysis.markdown,
                        height=420,
                        label_visibility="collapsed",
                    )
                with st.expander("Red-Flag Structured JSON", expanded=False):
                    if analysis.structured:
                        st.json(analysis.structured)
                    else:
                        st.info("No structured JSON was returned. Showing raw output below.")
                        st.code(analysis.markdown, language="markdown")
            else:
                st.markdown("#### Green-Flag Analysis Summary")
                if green_analysis.structured:
                    _render_green_analysis_summary(green_analysis.structured)
                else:
                    st.text_area(
                        "Green-Flag Analysis Summary",
                        value=green_analysis.markdown,
                        height=420,
                        label_visibility="collapsed",
                    )
                with st.expander("Green-Flag Structured JSON", expanded=False):
                    if green_analysis.structured:
                        st.json(green_analysis.structured)
                    else:
                        st.info("No structured JSON was returned. Showing raw output below.")
                        st.code(green_analysis.markdown, language="markdown")

        if st.session_state.get("hoa_extraction"):
            extraction = st.session_state["hoa_extraction"]
            total_pages = len(extraction.pages)
            st.markdown("#### Follow-Up Questions")
            range_start = st.session_state.get("hoa_page_start", 1)
            range_end = st.session_state.get("hoa_page_end", total_pages)
            question = st.chat_input(
                "Ask a follow-up question (e.g., 'Are rentals allowed?')",
                key="hoa_question",
            )
            if question:
                if range_start > range_end:
                    st.error("Adjust the page range to submit a question.")
                    return
                filtered_pages = [
                    page
                    for page in extraction.pages
                    if range_start <= page.page_number <= range_end
                ]
                context_text = build_page_context(
                    type(extraction)(
                        document_name=extraction.document_name,
                        pages=filtered_pages,
                    )
                )
                model_id = st.session_state.get("hoa_inference_profile")
                response = answer_question(
                    context_text,
                    question,
                    document_name=extraction.document_name,
                    model_id=model_id,
                )
                input_tokens = response.get("input_tokens")
                output_tokens = response.get("output_tokens")
                if not input_tokens or not output_tokens:
                    input_tokens = max(1, len(question.split()))
                    output_tokens = max(20, len(str(response).split()))
                _record_cost(
                    int(input_tokens),
                    int(output_tokens),
                    document_name=extraction.document_name,
                )

                query = VettingQuery(
                    question=question,
                    answer=response.get("answer", ""),
                    document_name=extraction.document_name,
                    page_numbers=response.get("page_numbers", []),
                    quoted_text=response.get("quoted_text", ""),
                )
                st.session_state["hoa_queries"].append(query)

        if st.session_state.get("hoa_queries"):
            st.markdown("#### Query History")
            for idx, query in enumerate(reversed(st.session_state["hoa_queries"]), start=1):
                with st.expander(f"Question {idx}: {query.question}", expanded=False):
                    pages = ", ".join(str(p) for p in query.page_numbers) or "—"
                    st.markdown(
                        f"**Answer:** {query.answer}\n\n"
                        f"**Document:** {query.document_name}\n\n"
                        f"**Pages:** {pages}\n\n"
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
