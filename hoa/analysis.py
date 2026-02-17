"""Bedrock-based analysis helpers for HOA document vetting."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, Any, List, Iterable, Callable, Sequence, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

import boto3


def _get_optimal_workers() -> int:
    """Calculate optimal number of workers based on CPU count."""
    cpu_count = os.cpu_count() or 1
    if cpu_count == 1:
        return 1
    # Divide by 2 (threads -> cores) then subtract 1
    return max(1, (cpu_count // 2) - 1)


@dataclass
class AnalysisResult:
    structured: Dict[str, Any]
    markdown: str
    raw_text: str
    input_tokens: int | None = None
    output_tokens: int | None = None


def _build_analysis_prompt(document_text: str) -> str:
    return (
        "You are an HOA covenant vetting assistant. Your purpose is risk discovery and decision support "
        "for a prospective homeowner. Use only the provided document text and do not add outside knowledge.\n\n"
        "Objective\n"
        "Review this document and identify any clauses that could materially restrict a homeowner’s rights, "
        "increase financial obligations, limit resale or rental options, or allow broad or discretionary "
        "enforcement by the HOA. Quote the exact language, explain why each item is a potential red flag, "
        "and categorize the risk (financial, lifestyle, legal, resale). Do not assume anything not explicitly stated.\n\n"
        "Red-Flag Categories and Checks\n"
        "- Financial exposure: Does this document permit special assessments, uncapped fee increases, fines, or liens? "
        "Cite the exact sections and explain homeowner impact.\n"
        "- Use & lifestyle restrictions: Identify any restrictions on renting, home businesses, parking, vehicles, pets, "
        "or architectural changes that could reasonably surprise a homeowner.\n"
        "- Governance & enforcement risk: Highlight any language granting the HOA discretionary or unilateral enforcement "
        "powers, limited appeal rights, or easy amendment thresholds.\n\n"
        "Guardrails\n"
        "Do NOT flag generic HOA membership or routine dues/assessments unless the text explicitly indicates unusual "
        "financial risk (for example, unlimited assessments, special assessments without caps, unilateral fee changes, "
        "liens or foreclosure powers).\n\n"
        "Return ONLY valid JSON with this exact shape (no extra text, no markdown, no backticks):\n"
        "{\n"
        "  \"executive_summary\": {\n"
        "    \"overall_strictness\": \"low|medium|high\",\n"
        "    \"summary\": \"...\"\n"
        "  },\n"
        "  \"flags\": [\n"
        "    {\n"
        "      \"category\": \"financial|lifestyle|legal|resale\",\n"
        "      \"severity\": \"low|medium|high\",\n"
        "      \"confidence\": \"explicit|inferred\",\n"
        "      \"title\": \"short title\",\n"
        "      \"quoted_text\": \"exact clause snippet\",\n"
        "      \"page_numbers\": [1,2],\n"
        "      \"explanation\": \"why this is concerning\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "If no issues are found, return an empty flags array and still include the executive_summary.\n\n"
        "Document text follows:\n"
        f"{document_text}"
    )


def _build_green_analysis_prompt(document_text: str) -> str:
    return (
        "You are an HOA covenant vetting assistant. Your purpose is to surface favorable, homeowner-friendly "
        "provisions for decision support. Use only the provided document text and do not add outside knowledge.\n\n"
        "Objective\n"
        "Review this document and identify any clauses that are favorable to homeowners, limit HOA discretion, "
        "cap financial exposure, protect owner rights, or preserve flexibility for use, resale, or rental. "
        "Quote the exact language, explain why each item is a positive or protective feature, and categorize "
        "the benefit (financial, lifestyle, governance, resale). Do not infer benefits not explicitly stated.\n\n"
        "Green-Flag Categories and Checks\n"
        "- Financial protections: Identify any limits on fee increases, caps on special assessments, notice "
        "requirements, or protections against liens or fines.\n"
        "- Use & flexibility: Highlight language that permits rentals, home offices, reasonable architectural "
        "changes, pets, or parking with minimal approval burden.\n"
        "- Governance & owner rights: Find clauses that require owner votes for rule changes, provide appeal rights, "
        "require due process, or restrict arbitrary enforcement.\n\n"
        "Safety / scope guardrail\n"
        "Base all findings strictly on the document text. Do not provide legal advice or assume protections beyond "
        "what is stated.\n\n"
        "Return ONLY valid JSON with this exact shape (no extra text, no markdown, no backticks):\n"
        "{\n"
        "  \"executive_summary\": {\n"
        "    \"overall_support\": \"low|medium|high\",\n"
        "    \"summary\": \"...\"\n"
        "  },\n"
        "  \"benefits\": [\n"
        "    {\n"
        "      \"category\": \"financial|lifestyle|governance|resale\",\n"
        "      \"strength\": \"low|medium|high\",\n"
        "      \"confidence\": \"explicit|inferred\",\n"
        "      \"title\": \"short title\",\n"
        "      \"quoted_text\": \"exact clause snippet\",\n"
        "      \"page_numbers\": [1,2],\n"
        "      \"explanation\": \"why this is beneficial\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "If no benefits are found, return an empty benefits array and still include the executive_summary.\n\n"
        "Document text follows:\n"
        f"{document_text}"
    )


def _build_qa_prompt(document_text: str, question: str, document_name: str) -> str:
    return (
        "You answer questions about an HOA document. Only use the provided text. "
        "Every answer must cite the document name and page numbers. If the answer is not "
        "in the document, respond that the answer is not found and leave quoted_text empty. "
        "Do not infer beyond the text.\n\n"
        "Answer formatting rules:\n"
        "- Provide ONLY the direct answer (no preamble, no 'According to the document', no disclaimers).\n"
        "- Prefer a concise bulleted list when multiple items apply.\n"
        "- If there is only one item, return a single short sentence without extra context.\n"
        "- Do not include the document name or page numbers in the answer text (those are separate fields).\n\n"
        "Citation rules:\n"
        "- Always populate page_numbers and quoted_text when the answer is found.\n"
        "- Provide the most relevant clause snippets in quoted_text (verbatim).\n"
        "- Use not_found=true only when there is no direct support in the provided text.\n\n"
        "Return ONLY valid JSON with this exact shape (no extra text):\n"
        "{\n"
        "  \"answer\": \"...\",\n"
        "  \"quoted_text\": \"...\",\n"
        "  \"page_numbers\": [1],\n"
        "  \"document_name\": \"...\",\n"
        "  \"confidence\": \"high|medium|low\",\n"
        "  \"not_found\": true|false\n"
        "}\n\n"
        f"Document name: {document_name}\n"
        f"Question: {question}\n\n"
        "Document text follows:\n"
        f"{document_text}"
    )


def _build_question_decomposition_prompt(question: str) -> str:
    return (
        "You expand HOA document questions into structured search requirements. "
        "Return ONLY valid JSON (no extra text).\n\n"
        "Extract the user's intent into fields that can be used for keyword matching. "
        "Include exclusions ONLY when you are confident they are irrelevant to the user intent.\n\n"
        "Return this JSON shape:\n"
        "{\n"
        "  \"intent\": \"short intent label\",\n"
        "  \"entities\": [""],\n"
        "  \"events\": [""],\n"
        "  \"obligations\": [""],\n"
        "  \"financial_terms\": [""],\n"
        "  \"time_scope\": \"one-time|recurring|time-bound|unspecified\",\n"
        "  \"synonyms\": [""],\n"
        "  \"exclusions\": [""],\n"
        "  \"exclusion_confident\": true|false\n"
        "}\n\n"
        "Rules:\n"
        "- Use empty arrays when nothing applies.\n"
        "- Set exclusion_confident=true only when you are sure the exclusions should be ignored.\n"
        "- If exclusion_confident=false, leave exclusions empty.\n\n"
        f"Question: {question.strip()}"
    )


def _extract_answer_type(question: str) -> tuple[str, str | None]:
    match = re.match(r"\s*Answer type:\s*([^\.]+)\.\s*(.*)", question, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(2).strip(), match.group(1).strip()
    return question.strip(), None


def _normalize_terms(terms: Iterable[str]) -> List[str]:
    cleaned: List[str] = []
    for term in terms:
        if not term:
            continue
        normalized = re.sub(r"\s+", " ", str(term).strip().lower())
        if normalized and normalized not in cleaned:
            cleaned.append(normalized)
    return cleaned


def _decompose_question(
    question: str,
    *,
    model_id: str,
    region_name: str | None = None,
) -> Dict[str, Any]:
    response = invoke_bedrock_json(
        _build_question_decomposition_prompt(question),
        model_id=model_id,
        region_name=region_name,
        max_tokens=700,
    )
    raw = response.get("raw", "")
    parsed = _try_extract_json(raw)
    if not isinstance(parsed, dict):
        return {}
    exclusions = parsed.get("exclusions") if parsed.get("exclusion_confident") else []
    parsed["exclusions"] = exclusions if isinstance(exclusions, list) else []
    return parsed


def _build_search_requirements(profile: Dict[str, Any]) -> Dict[str, List[str]]:
    intent = profile.get("intent") if isinstance(profile.get("intent"), str) else ""
    entities = profile.get("entities") if isinstance(profile.get("entities"), list) else []
    events = profile.get("events") if isinstance(profile.get("events"), list) else []
    obligations = profile.get("obligations") if isinstance(profile.get("obligations"), list) else []
    financial_terms = profile.get("financial_terms") if isinstance(profile.get("financial_terms"), list) else []
    synonyms = profile.get("synonyms") if isinstance(profile.get("synonyms"), list) else []
    time_scope = profile.get("time_scope") if isinstance(profile.get("time_scope"), str) else ""
    exclusions = profile.get("exclusions") if isinstance(profile.get("exclusions"), list) else []

    must_terms = _normalize_terms([intent, *entities, *events])
    boost_terms = _normalize_terms([*obligations, *financial_terms, *synonyms, time_scope])
    if intent.lower() in {"fees", "fee", "costs", "charges", "assessments", "payments"}:
        boost_terms = _normalize_terms(
            [
                *boost_terms,
                "working capital",
                "capital contribution",
                "initial contribution",
                "initial capital",
                "initial working",
                "contribution",
                "assessment",
                "closing",
                "settlement",
                "at the time of settlement",
                "at the time of closing",
                "start-up costs",
                "working fund",
                "deposit",
                "fee",
                "resale fee",
                "account setup",
            ]
        )
    exclusion_terms = _normalize_terms(exclusions)

    return {
        "must_terms": must_terms,
        "boost_terms": boost_terms,
        "exclusions": exclusion_terms,
    }


def _score_chunk_context(context: str, requirements: Dict[str, List[str]]) -> Dict[str, int]:
    text = context.lower()

    def _count_hits(terms: List[str]) -> int:
        return sum(1 for term in terms if term and term in text)

    must_terms = requirements.get("must_terms", [])
    boost_terms = requirements.get("boost_terms", [])
    exclusions = requirements.get("exclusions", [])

    must_hits = _count_hits(must_terms)
    boost_hits = _count_hits(boost_terms)
    exclusion_hits = _count_hits(exclusions)

    score = (must_hits * 3) + boost_hits - (exclusion_hits * 4)
    if must_terms and must_hits == 0:
        score -= 2
    return {
        "score": score,
        "must_hits": must_hits,
        "boost_hits": boost_hits,
        "exclusion_hits": exclusion_hits,
    }


def _build_followup_synthesis_prompt(
    *,
    question: str,
    answer_type: str,
    excerpts: List[Dict[str, Any]],
) -> str:
    excerpt_lines = []
    for excerpt in excerpts:
        pages = ", ".join(str(p) for p in excerpt.get("page_numbers", [])) or "—"
        quoted = excerpt.get("quoted_text", "").strip()
        if not quoted:
            continue
        excerpt_lines.append(f"Pages {pages}: {quoted}")
    excerpts_block = "\n".join(excerpt_lines) if excerpt_lines else "(no excerpts)"

    return (
        "You answer HOA document follow-up questions using ONLY the provided excerpts. "
        "Do not add information that is not in the excerpts.\n\n"
        f"Answer type: {answer_type}\n"
        f"Question: {question}\n\n"
        "Answer rules:\n"
        "- Summarized: list ALL applicable items, preferably as bullets. Include amounts and timing if stated.\n"
        "- Yes/No + cite: respond Yes/No first, then a short citation-based sentence.\n"
        "- Clause lookup: return only the most relevant clause excerpt(s) verbatim.\n"
        "- Do not include page numbers or document names in the answer.\n\n"
        "Return ONLY valid JSON with this shape (no extra text, no markdown):\n"
        "{\n"
        "  \"answer\": \"...\"\n"
        "}\n\n"
        f"Excerpts:\n{excerpts_block}"
    )


def _synthesize_followup_answer(
    *,
    question: str,
    answer_type: str,
    excerpts: List[Dict[str, Any]],
    model_id: str,
    region_name: str | None = None,
) -> str:
    response = invoke_bedrock_json(
        _build_followup_synthesis_prompt(
            question=question,
            answer_type=answer_type,
            excerpts=excerpts,
        ),
        model_id=model_id,
        region_name=region_name,
        max_tokens=900,
    )
    raw = response.get("raw", "")
    parsed = _try_extract_json(raw)
    if isinstance(parsed, dict) and parsed.get("answer"):
        return str(parsed.get("answer")).strip()
    return raw.strip().lstrip("{")


def invoke_bedrock_json(
    prompt: str,
    *,
    model_id: str,
    region_name: str | None = None,
    max_tokens: int = 4096,
) -> Dict[str, Any]:
    client = boto3.client("bedrock-runtime", region_name=region_name)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": max_tokens,
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


def analyze_document(
    document_text: str,
    *,
    model_id: str,
    region_name: str | None = None,
) -> AnalysisResult:
    response = invoke_bedrock_json(
        _build_analysis_prompt(document_text),
        model_id=model_id,
        region_name=region_name,
    )
    raw = response.get("raw", "")
    usage = response.get("usage", {})
    structured = _try_parse_json(raw)
    markdown = _build_markdown(structured, raw)
    return AnalysisResult(
        structured=structured,
        markdown=markdown,
        raw_text=raw,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
    )


def analyze_document_chunked(
    pages: Iterable[str],
    *,
    model_id: str,
    region_name: str | None = None,
    max_pages_per_chunk: int = 12,
    on_progress: Callable[[int, int], None] | None = None,
    max_workers: int | None = None,
) -> AnalysisResult:
    """Run analysis over paginated text in parallel, merge results into a single summary."""
    if max_workers is None:
        max_workers = _get_optimal_workers()
    chunks: List[str] = []
    current: List[str] = []
    for idx, page_text in enumerate(pages, start=1):
        current.append(f"--- Page {idx} ---\n{page_text}")
        if idx % max_pages_per_chunk == 0:
            chunks.append("\n\n".join(current))
            current = []
    if current:
        chunks.append("\n\n".join(current))

    summaries: List[str] = []
    all_flags: List[Dict[str, Any]] = []
    strictness_values: List[str] = []
    failed_chunks = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_chunks = len(chunks)
    
    completed_count = 0

    def process_chunk(chunk: str) -> Dict[str, Any]:
        """Process a single chunk and return results."""
        response = invoke_bedrock_json(
            _build_analysis_prompt(chunk),
            model_id=model_id,
            region_name=region_name,
        )
        return response
    
    # Process chunks in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_chunk = {executor.submit(process_chunk, chunk): chunk for chunk in chunks}
        
        for future in as_completed(future_to_chunk):
            try:
                response = future.result()
                completed_count += 1
                if on_progress:
                    on_progress(completed_count, total_chunks)
                raw = response.get("raw", "")
                usage = response.get("usage", {})
                
                if usage.get("input_tokens"):
                    total_input_tokens += int(usage["input_tokens"])
                if usage.get("output_tokens"):
                    total_output_tokens += int(usage["output_tokens"])
                
                structured = _try_extract_json(raw)
                if structured:
                    summary = structured.get("executive_summary", {})
                    summary_text = summary.get("summary")
                    if summary_text:
                        summaries.append(summary_text)
                    strictness = summary.get("overall_strictness")
                    if strictness:
                        strictness_values.append(strictness)
                    all_flags.extend(structured.get("flags", []))
                else:
                    failed_chunks += 1
            except Exception as exc:
                failed_chunks += 1
                print(f"Chunk processing error: {exc}")

    summary_text = _merge_summaries(summaries)
    if failed_chunks:
        summary_text = " ".join(
            [
                summary_text,
                f"(Note: {failed_chunks} chunk(s) could not be parsed and may be underrepresented.)",
            ]
        ).strip()

    merged = {
        "executive_summary": {
            "overall_strictness": _merge_strictness(strictness_values),
            "summary": summary_text,
        },
        "flags": _dedupe_flags(all_flags),
    }
    markdown = _build_markdown(merged, json.dumps(merged, indent=2))
    return AnalysisResult(
        structured=merged,
        markdown=markdown,
        raw_text=json.dumps(merged, indent=2),
        input_tokens=total_input_tokens or None,
        output_tokens=total_output_tokens or None,
    )


def analyze_document_chunked_green(
    pages: Iterable[str],
    *,
    model_id: str,
    region_name: str | None = None,
    max_pages_per_chunk: int = 12,
    on_progress: Callable[[int, int], None] | None = None,
    max_workers: int | None = None,
) -> AnalysisResult:
    """Run green-flag analysis over paginated text in parallel, merge results into a single summary."""
    if max_workers is None:
        max_workers = _get_optimal_workers()
    chunks: List[str] = []
    current: List[str] = []
    for idx, page_text in enumerate(pages, start=1):
        current.append(f"--- Page {idx} ---\n{page_text}")
        if idx % max_pages_per_chunk == 0:
            chunks.append("\n\n".join(current))
            current = []
    if current:
        chunks.append("\n\n".join(current))

    summaries: List[str] = []
    all_benefits: List[Dict[str, Any]] = []
    support_values: List[str] = []
    failed_chunks = 0
    total_input_tokens = 0
    total_output_tokens = 0
    total_chunks = len(chunks)
    
    completed_count = 0

    def process_chunk(chunk: str) -> Dict[str, Any]:
        """Process a single chunk and return results."""
        response = invoke_bedrock_json(
            _build_green_analysis_prompt(chunk),
            model_id=model_id,
            region_name=region_name,
        )
        return response
    
    # Process chunks in parallel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_chunk = {executor.submit(process_chunk, chunk): chunk for chunk in chunks}
        
        for future in as_completed(future_to_chunk):
            try:
                response = future.result()
                completed_count += 1
                if on_progress:
                    on_progress(completed_count, total_chunks)
                raw = response.get("raw", "")
                usage = response.get("usage", {})
                
                if usage.get("input_tokens"):
                    total_input_tokens += int(usage["input_tokens"])
                if usage.get("output_tokens"):
                    total_output_tokens += int(usage["output_tokens"])
                
                structured = _try_extract_json(raw)
                if structured:
                    summary = structured.get("executive_summary", {})
                    summary_text = summary.get("summary")
                    if summary_text:
                        summaries.append(summary_text)
                    support = summary.get("overall_support")
                    if support:
                        support_values.append(support)
                    all_benefits.extend(structured.get("benefits", []))
                else:
                    failed_chunks += 1
            except Exception as exc:
                failed_chunks += 1
                print(f"Chunk processing error: {exc}")

    summary_text = _merge_summaries(summaries)
    if failed_chunks:
        summary_text = " ".join(
            [
                summary_text,
                f"(Note: {failed_chunks} chunk(s) could not be parsed and may be underrepresented.)",
            ]
        ).strip()

    merged = {
        "executive_summary": {
            "overall_support": _merge_strictness(support_values),
            "summary": summary_text,
        },
        "benefits": _dedupe_flags(all_benefits),
    }
    markdown = _build_green_markdown(merged, json.dumps(merged, indent=2))
    return AnalysisResult(
        structured=merged,
        markdown=markdown,
        raw_text=json.dumps(merged, indent=2),
        input_tokens=total_input_tokens or None,
        output_tokens=total_output_tokens or None,
    )


def answer_question(
    document_text: str,
    question: str,
    *,
    document_name: str,
    model_id: str,
    region_name: str | None = None,
) -> Dict[str, Any]:
    response = invoke_bedrock_json(
        _build_qa_prompt(document_text, question, document_name),
        model_id=model_id,
        region_name=region_name,
    )
    raw = response.get("raw", "")
    usage = response.get("usage", {})
    parsed = _try_extract_json(raw)
    parsed.setdefault("answer", raw.strip())
    parsed.setdefault("quoted_text", "")
    parsed.setdefault("page_numbers", [])
    parsed.setdefault("document_name", document_name)
    parsed.setdefault("confidence", "low")
    parsed.setdefault("not_found", False if parsed.get("quoted_text") else True)
    if usage.get("input_tokens"):
        parsed["input_tokens"] = usage.get("input_tokens")
    if usage.get("output_tokens"):
        parsed["output_tokens"] = usage.get("output_tokens")
    return parsed


def answer_question_chunked(
    pages: Sequence[Tuple[int, str]],
    question: str,
    *,
    document_name: str,
    model_id: str,
    region_name: str | None = None,
    max_pages_per_chunk: int = 2,
    on_progress: Callable[[int, int], None] | None = None,
) -> Dict[str, Any]:
    """Answer a question by scanning smaller page chunks to avoid model input limits."""
    if not pages:
        return {
            "answer": "No document text available for the selected range.",
            "quoted_text": "",
            "page_numbers": [],
            "document_name": document_name,
            "confidence": "low",
            "not_found": True,
        }

    chunks: List[List[Tuple[int, str]]] = []
    current: List[Tuple[int, str]] = []
    for page in pages:
        current.append(page)
        if len(current) >= max_pages_per_chunk:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)

    raw_question, answer_type = _extract_answer_type(question)
    if not answer_type:
        answer_type = "Summarized"
    decomposition = _decompose_question(
        raw_question,
        model_id=model_id,
        region_name=region_name,
    )
    requirements = _build_search_requirements(decomposition) if decomposition else {}

    chunk_entries: List[Dict[str, Any]] = []
    for chunk_pages in chunks:
        chunk_context = "\n\n".join(
            f"--- Page {page_number} ---\n{page_text}"
            for page_number, page_text in chunk_pages
        )
        score_data = _score_chunk_context(chunk_context, requirements) if requirements else {
            "score": 0,
            "must_hits": 0,
            "boost_hits": 0,
            "exclusion_hits": 0,
        }
        chunk_entries.append(
            {
                "pages": chunk_pages,
                "context": chunk_context,
                "score_data": score_data,
            }
        )

    sorted_entries = sorted(
        chunk_entries,
        key=lambda entry: entry["score_data"]["score"],
        reverse=True,
    )
    top_score = sorted_entries[0]["score_data"]["score"] if sorted_entries else 0
    use_all_chunks = not requirements or top_score <= 0
    max_candidates = min(14, len(sorted_entries))
    candidates = sorted_entries if use_all_chunks else sorted_entries[:max_candidates]

    best_response: Dict[str, Any] | None = None
    all_page_numbers: List[int] = []
    all_quotes: List[str] = []
    total_input_tokens = 0
    total_output_tokens = 0
    confidence_rank = {"high": 3, "medium": 2, "low": 1}
    response_excerpts: List[Dict[str, Any]] = []

    total_chunks = len(candidates)
    for chunk_index, entry in enumerate(candidates, start=1):
        if on_progress:
            on_progress(chunk_index, total_chunks)
        response = answer_question(
            entry["context"],
            question,
            document_name=document_name,
            model_id=model_id,
            region_name=region_name,
        )
        if response.get("input_tokens"):
            total_input_tokens += int(response["input_tokens"])
        if response.get("output_tokens"):
            total_output_tokens += int(response["output_tokens"])

        quoted_text = (response.get("quoted_text") or "").strip()
        page_numbers = response.get("page_numbers") or []
        if quoted_text:
            all_quotes.append(quoted_text)
        if page_numbers:
            all_page_numbers.extend(page_numbers)
        if quoted_text or page_numbers:
            response_excerpts.append(
                {
                    "quoted_text": quoted_text,
                    "page_numbers": page_numbers,
                    "score": entry["score_data"]["score"],
                }
            )

        if quoted_text or page_numbers:
            if best_response is None:
                best_response = response
                best_response["_score"] = entry["score_data"]["score"]
            else:
                current_score = entry["score_data"]["score"]
                best_score = best_response.get("_score", 0)
                current_conf = confidence_rank.get(response.get("confidence", "low"), 1)
                best_conf = confidence_rank.get(best_response.get("confidence", "low"), 1)
                if current_score > best_score or (
                    current_score == best_score and current_conf > best_conf
                ):
                    best_response = response
                    best_response["_score"] = current_score

    if best_response is None:
        return {
            "answer": "The answer was not found in the selected page range.",
            "quoted_text": "",
            "page_numbers": [],
            "document_name": document_name,
            "confidence": "low",
            "not_found": True,
            "input_tokens": total_input_tokens or None,
            "output_tokens": total_output_tokens or None,
        }

    unique_excerpts: List[Dict[str, Any]] = []
    seen_quotes = set()
    for excerpt in response_excerpts:
        quote_key = excerpt.get("quoted_text", "").strip().lower()
        if not quote_key or quote_key in seen_quotes:
            continue
        seen_quotes.add(quote_key)
        unique_excerpts.append(excerpt)

    synthesized_answer = _synthesize_followup_answer(
        question=raw_question,
        answer_type=answer_type,
        excerpts=unique_excerpts,
        model_id=model_id,
        region_name=region_name,
    )

    if synthesized_answer.startswith("\"{") or synthesized_answer.startswith("{"):
        parsed_answer = _try_extract_json(synthesized_answer)
        if isinstance(parsed_answer, dict) and parsed_answer.get("answer"):
            synthesized_answer = str(parsed_answer.get("answer")).strip()

    merged = dict(best_response)
    merged["answer"] = synthesized_answer or merged.get("answer", "")
    merged["quoted_text"] = "\n\n".join(
        quote for quote in all_quotes if quote
    ).strip()
    merged["page_numbers"] = sorted({int(p) for p in all_page_numbers if str(p).isdigit()})
    merged["not_found"] = False if merged.get("quoted_text") else True
    merged["input_tokens"] = total_input_tokens or merged.get("input_tokens")
    merged["output_tokens"] = total_output_tokens or merged.get("output_tokens")
    merged.pop("_score", None)
    return merged


def _try_parse_json(text: str) -> Dict[str, Any]:
    text = text.strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


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


def _build_markdown(structured: Dict[str, Any], raw: str) -> str:
    if not structured:
        raw_block = "\n".join([
            "```",
            raw.strip(),
            "```",
        ])
        return "".join(
            [
                "### Executive Overview\n",
                "- Analysis output was not valid JSON.\n\n",
                "### Raw Output\n",
                raw_block,
                "\n\n",
                "_This analysis is informational only and not legal advice. Consult a qualified professional for legal guidance._",
            ]
        )

    summary = structured.get("executive_summary", {})
    flags = structured.get("flags", [])

    lines = ["### Executive Overview"]
    strictness = summary.get("overall_strictness", "—")
    summary_text = summary.get("summary", "")
    lines.append(f"- **Overall strictness:** {strictness}")
    if summary_text:
        lines.append(f"- {summary_text}")

    if flags:
        lines.append("\n### Flagged Clauses")
        for flag in flags:
            title = flag.get("title", "Flag")
            category = flag.get("category", "—")
            severity = flag.get("severity", "—")
            confidence = flag.get("confidence", "—")
            pages = ", ".join(str(p) for p in flag.get("page_numbers", [])) or "—"
            quoted = flag.get("quoted_text", "")
            explanation = flag.get("explanation", "")
            lines.append(
                f"- **{title}** ({category}, {severity}, {confidence}) — pages {pages}\n"
                f"  - _{quoted}_\n  - {explanation}"
            )

    lines.append(
        "\n_This analysis is informational only and not legal advice. Consult a qualified professional for legal guidance._"
    )
    return "\n".join(lines)


def _build_green_markdown(structured: Dict[str, Any], raw: str) -> str:
    if not structured:
        raw_block = "\n".join([
            "```",
            raw.strip(),
            "```",
        ])
        return "".join(
            [
                "### Executive Overview\n",
                "- Analysis output was not valid JSON.\n\n",
                "### Raw Output\n",
                raw_block,
                "\n\n",
                "_This analysis is informational only and not legal advice. Consult a qualified professional for legal guidance._",
            ]
        )

    summary = structured.get("executive_summary", {})
    benefits = structured.get("benefits", [])

    lines = ["### Executive Overview"]
    support = summary.get("overall_support", "—")
    summary_text = summary.get("summary", "")
    lines.append(f"- **Overall support:** {support}")
    if summary_text:
        lines.append(f"- {summary_text}")

    if benefits:
        lines.append("\n### Green Flag Highlights")
        for benefit in benefits:
            title = benefit.get("title", "Benefit")
            category = benefit.get("category", "—")
            strength = benefit.get("strength", "—")
            confidence = benefit.get("confidence", "—")
            pages = ", ".join(str(p) for p in benefit.get("page_numbers", [])) or "—"
            quoted = benefit.get("quoted_text", "")
            explanation = benefit.get("explanation", "")
            lines.append(
                f"- **{title}** ({category}, {strength}, {confidence}) — pages {pages}\n"
                f"  - _{quoted}_\n  - {explanation}"
            )

    lines.append(
        "\n_This analysis is informational only and not legal advice. Consult a qualified professional for legal guidance._"
    )
    return "\n".join(lines)


def _merge_strictness(values: List[str]) -> str:
    if not values:
        return "—"
    priority = {"high": 3, "medium": 2, "low": 1}
    return max(values, key=lambda v: priority.get(v, 0))


def _merge_summaries(summaries: List[str]) -> str:
    cleaned = [summary for summary in summaries if summary]
    if not cleaned:
        return ""
    return " ".join(cleaned)


def _dedupe_flags(flags: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    unique = []
    for flag in flags:
        title = flag.get("title", "").strip().lower()
        quoted = flag.get("quoted_text", "").strip().lower()
        key = (title, quoted)
        if key in seen:
            continue
        seen.add(key)
        unique.append(flag)
    return unique
