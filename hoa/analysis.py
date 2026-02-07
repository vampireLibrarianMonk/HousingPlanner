"""Bedrock-based analysis helpers for HOA document vetting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Any, List, Iterable, Callable

import boto3


@dataclass
class AnalysisResult:
    structured: Dict[str, Any]
    markdown: str
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


def _build_qa_prompt(document_text: str, question: str, document_name: str) -> str:
    return (
        "You answer questions about an HOA document. Only use the provided text. "
        "Every answer must cite the document name and page numbers. If the answer is not "
        "in the document, say so. Include a short disclaimer that this is informational only.\n\n"
        "Return JSON with this shape:\n"
        "{\n"
        "  \"answer\": \"...\",\n"
        "  \"quoted_text\": \"...\",\n"
        "  \"page_numbers\": [1],\n"
        "  \"document_name\": \"...\"\n"
        "}\n\n"
        f"Document name: {document_name}\n"
        f"Question: {question}\n\n"
        "Document text follows:\n"
        f"{document_text}"
    )


def invoke_bedrock_json(
    prompt: str,
    *,
    model_id: str,
    region_name: str | None = None,
) -> Dict[str, Any]:
    client = boto3.client("bedrock-runtime", region_name=region_name)
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1200,
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
) -> AnalysisResult:
    """Run analysis over paginated text, merge results into a single summary."""
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
    for index, chunk in enumerate(chunks, start=1):
        if on_progress:
            on_progress(index, total_chunks)
        response = invoke_bedrock_json(
            _build_analysis_prompt(chunk),
            model_id=model_id,
            region_name=region_name,
        )
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
    parsed = _try_parse_json(raw)
    parsed.setdefault("answer", raw.strip())
    if usage.get("input_tokens"):
        parsed["input_tokens"] = usage.get("input_tokens")
    if usage.get("output_tokens"):
        parsed["output_tokens"] = usage.get("output_tokens")
    return parsed


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
