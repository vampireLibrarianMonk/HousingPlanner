"""Streamlit UI for HOA document vetting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List
import os

import streamlit as st

from config.pricing import (
    CLAUDE_INFERENCE_PROFILES,
    PRICING_REGISTRY,
    PRICING_VERSION,
    estimate_request_cost,
)
from hoa.analysis import analyze_document_chunked, answer_question
from hoa.extraction import (
    build_page_context,
    start_textract_job,
    poll_textract_job,
    cleanup_textract_job,
    blocks_to_extraction,
)
from profile.identity import (
    get_owner_sub,
    bucket_name_for_owner,
    get_storage_bucket_prefix,
)
import boto3

S3_STORAGE_PER_GB_MONTH = 0.023
S3_PUT_REQUEST_PER_1000 = 0.005
S3_GET_REQUEST_PER_1000 = 0.0004
TEXTRACT_TEXT_DETECTION_PER_PAGE = 0.0015
GB_IN_BYTES = 1024 ** 3
HOURS_IN_MONTH = 720.0
S3_STAGING_HOURS = 0.25


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
    if "hoa_cost_by_document" not in st.session_state:
        st.session_state["hoa_cost_by_document"] = {}
    if "hoa_cost_breakdown" not in st.session_state:
        st.session_state["hoa_cost_breakdown"] = {}


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

        if upload:
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
            else:
                st.info("Cost breakdown will appear after analysis completes.")

        analyze_button = st.button("Run Red-Flag Analysis", key="hoa_analyze")
        retry_button = st.button(
            "Retry Textract Polling",
            key="hoa_textract_retry",
            disabled=not st.session_state.get("hoa_textract_timeout"),
        )

        if analyze_button and upload:
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
                cleanup_textract_job(s3_key, bucket_name=bucket_name)
                st.session_state["hoa_textract_job_id"] = None
                st.session_state["hoa_textract_s3_key"] = None

                extraction = blocks_to_extraction(blocks, upload.name)
                st.session_state["hoa_extraction"] = extraction
                page_count = len(extraction.pages)
                st.success(f"Document type: PDF · Pages detected: {page_count}")
                context_text = build_page_context(extraction)
                doc_name = extraction.document_name
                doc_size = st.session_state.get("hoa_documents", [{}])[0].get("size", 0)
                storage_cost = (doc_size / GB_IN_BYTES) * (S3_STAGING_HOURS / HOURS_IN_MONTH) * S3_STORAGE_PER_GB_MONTH
                request_cost = ((2 * S3_PUT_REQUEST_PER_1000) + (2 * S3_GET_REQUEST_PER_1000)) / 1000.0
                textract_cost = page_count * TEXTRACT_TEXT_DETECTION_PER_PAGE
                _set_cost_component(doc_name, "s3_storage", storage_cost)
                _set_cost_component(doc_name, "s3_requests", request_cost)
                _set_cost_component(doc_name, "textract", textract_cost)

                model_id = st.session_state.get("hoa_inference_profile")
                analysis_status = st.empty()

                def _on_analysis_progress(current: int, total: int) -> None:
                    analysis_status.info(f"Analyzing chunk {current} of {total}...")

                with st.spinner("Running red-flag analysis..."):
                    analysis = analyze_document_chunked(
                        [page.text for page in extraction.pages],
                        model_id=model_id,
                        on_progress=_on_analysis_progress,
                    )
                analysis_status.success("Analysis complete.")
                st.session_state["hoa_analysis"] = analysis
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
                st.rerun()
            except TimeoutError:
                st.session_state["hoa_textract_timeout"] = True
                st.warning(
                    "Textract is still processing. Click 'Retry Textract Polling' to continue without re-uploading."
                )
            except Exception as exc:
                st.error(f"Textract failed: {exc}")

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
                    cleanup_textract_job(s3_key, bucket_name=bucket_name)
                    st.session_state["hoa_textract_job_id"] = None
                    st.session_state["hoa_textract_s3_key"] = None
                    st.session_state["hoa_textract_bucket"] = None
                    st.session_state["hoa_textract_timeout"] = False

                    extraction = blocks_to_extraction(blocks, upload.name)
                    st.session_state["hoa_extraction"] = extraction
                    page_count = len(extraction.pages)
                    st.success(f"Document type: PDF · Pages detected: {page_count}")
                    context_text = build_page_context(extraction)
                    doc_name = extraction.document_name
                    doc_size = st.session_state.get("hoa_documents", [{}])[0].get("size", 0)
                    storage_cost = (doc_size / GB_IN_BYTES) * (S3_STAGING_HOURS / HOURS_IN_MONTH) * S3_STORAGE_PER_GB_MONTH
                    request_cost = ((2 * S3_PUT_REQUEST_PER_1000) + (2 * S3_GET_REQUEST_PER_1000)) / 1000.0
                    textract_cost = page_count * TEXTRACT_TEXT_DETECTION_PER_PAGE
                    _set_cost_component(doc_name, "s3_storage", storage_cost)
                    _set_cost_component(doc_name, "s3_requests", request_cost)
                    _set_cost_component(doc_name, "textract", textract_cost)

                    model_id = st.session_state.get("hoa_inference_profile")
                    analysis_status = st.empty()

                    def _on_analysis_progress(current: int, total: int) -> None:
                        analysis_status.info(f"Analyzing chunk {current} of {total}...")

                    with st.spinner("Running red-flag analysis..."):
                        analysis = analyze_document_chunked(
                            [page.text for page in extraction.pages],
                            model_id=model_id,
                            on_progress=_on_analysis_progress,
                        )
                    analysis_status.success("Analysis complete.")
                    st.session_state["hoa_analysis"] = analysis
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
                    st.rerun()
                except TimeoutError:
                    st.session_state["hoa_textract_timeout"] = True
                    st.warning(
                        "Textract is still processing. Click 'Retry Textract Polling' to continue without re-uploading."
                    )
                except Exception as exc:
                    st.error(f"Textract failed: {exc}")

        analysis = st.session_state.get("hoa_analysis")
        if analysis:
            st.markdown("#### Analysis Summary")
            if analysis.structured:
                _render_analysis_summary(analysis.structured)
            else:
                st.text_area(
                    "Analysis Summary",
                    value=analysis.markdown,
                    height=420,
                    label_visibility="collapsed",
                )
            with st.expander("Structured JSON", expanded=False):
                if analysis.structured:
                    st.json(analysis.structured)
                else:
                    st.info("No structured JSON was returned. Showing raw output below.")
                    st.code(analysis.markdown, language="markdown")

        if st.session_state.get("hoa_extraction"):
            extraction = st.session_state["hoa_extraction"]
            total_pages = len(extraction.pages)
            st.markdown("#### Follow-Up Questions")
            range_col1, range_col2 = st.columns(2)
            with range_col1:
                range_start = st.number_input(
                    "Start page",
                    min_value=1,
                    max_value=max(1, total_pages),
                    value=1,
                    step=1,
                    key="hoa_page_start",
                )
            with range_col2:
                range_end = st.number_input(
                    "End page",
                    min_value=1,
                    max_value=max(1, total_pages),
                    value=total_pages,
                    step=1,
                    key="hoa_page_end",
                )
            if range_start > range_end:
                st.warning("Start page cannot be greater than end page.")
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
        "<div style='max-height: 480px; overflow-y: auto; padding: 12px; border: 1px solid #E0E0E0; border-radius: 8px; background-color: #FAFAFA;'>",
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
