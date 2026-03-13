"""PDF ingestion for checklist updates (Textract -> Bedrock JSON actions)."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import boto3

from hoa.extraction import (
    blocks_to_extraction,
    cleanup_textract_job,
    poll_textract_job,
    start_textract_job,
)
from profile.identity import bucket_name_for_owner, get_owner_sub, get_storage_bucket_prefix

DEFAULT_HAIKU_INFERENCE_MODEL_ID = "us.anthropic.claude-3-haiku-20240307-v1:0"


def _extract_json_object(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _build_prompt(extracted_text: str, checklist_rows: List[Dict[str, Any]]) -> str:
    rows_json = json.dumps(checklist_rows, ensure_ascii=False)
    return (
        "You extract actionable checklist updates from home-buying documents. "
        "Use ONLY the provided text and existing checklist context.\n\n"
        "Return ONLY valid JSON in this exact shape:\n"
        "{\n"
        "  \"summary\": \"short summary\",\n"
        "  \"actions\": [\n"
        "    {\n"
        "      \"type\": \"add|update\",\n"
        "      \"label\": \"task name\",\n"
        "      \"status\": \"not_started|in_progress|done\",\n"
        "      \"due_date\": \"YYYY-MM-DD or null\",\n"
        "      \"category\": \"category text\",\n"
        "      \"notes\": \"optional notes\"\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        "Rules:\n"
        "- Do not invent facts not present in extracted text.\n"
        "- Prefer update when a checklist item already exists semantically.\n"
        "- Use add only when no existing checklist item reasonably matches.\n"
        "- If unsure, omit action.\n\n"
        f"Existing checklist rows:\n{rows_json}\n\n"
        f"Extracted document text:\n{extracted_text}"
    )


def _invoke_bedrock_json(prompt: str, model_id: str) -> Dict[str, Any]:
    client = boto3.client("bedrock-runtime")
    body = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1800,
        "temperature": 0.1,
        "messages": [{"role": "user", "content": prompt}],
    }
    response = client.invoke_model(modelId=model_id, body=json.dumps(body))
    payload = json.loads(response["body"].read())
    content = payload.get("content", [])
    raw_text = "".join(part.get("text", "") for part in content if part.get("type") == "text")
    usage = payload.get("usage") or {}
    parsed = _extract_json_object(raw_text)
    return {
        "parsed": parsed,
        "raw": raw_text,
        "input_tokens": usage.get("input_tokens") or 0,
        "output_tokens": usage.get("output_tokens") or 0,
    }


def _normalize_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type", "")).strip().lower()
        if action_type not in {"add", "update"}:
            continue
        label = str(action.get("label", "")).strip()
        if not label:
            continue
        status = str(action.get("status", "not_started") or "not_started").strip().lower()
        if status not in {"not_started", "in_progress", "done"}:
            status = "not_started"
        due_date = action.get("due_date")
        if due_date in {"", "null", "None"}:
            due_date = None
        normalized.append(
            {
                "type": action_type,
                "label": label,
                "status": status,
                "due_date": due_date,
                "category": str(action.get("category", "") or ""),
                "notes": str(action.get("notes", "") or ""),
            }
        )
    return normalized


def extract_actions_from_pdf(
    file_bytes: bytes,
    file_name: str,
    checklist_rows: List[Dict[str, Any]],
    *,
    model_id: str | None = None,
) -> Dict[str, Any]:
    resolved_model_id = model_id or DEFAULT_HAIKU_INFERENCE_MODEL_ID
    owner_sub = get_owner_sub()
    bucket_prefix = get_storage_bucket_prefix() or "checklist"
    if not owner_sub or not bucket_prefix:
        raise RuntimeError("Storage bucket is not configured. Set OwnerSub and STORAGE_BUCKET_PREFIX.")

    bucket_name = bucket_name_for_owner(owner_sub, bucket_prefix)
    job_id, s3_key = start_textract_job(file_bytes, file_name, bucket_name=bucket_name)
    try:
        blocks = poll_textract_job(job_id, poll_delay_seconds=1.5, max_polls=120)
    finally:
        cleanup_textract_job(s3_key, bucket_name=bucket_name)

    extraction = blocks_to_extraction(blocks, file_name)
    extracted_text = "\n\n".join(page.text for page in extraction.pages if page.text).strip()
    if not extracted_text:
        return {
            "summary": "No extractable text found in PDF.",
            "actions": [],
            "input_tokens": 0,
            "output_tokens": 0,
        }

    llm_result = _invoke_bedrock_json(
        _build_prompt(extracted_text, checklist_rows),
        model_id=resolved_model_id,
    )
    parsed = llm_result.get("parsed") or {}
    actions = _normalize_actions(parsed.get("actions", []))
    summary = str(parsed.get("summary", "") or "Parsed PDF and proposed checklist updates.")
    return {
        "summary": summary,
        "actions": actions,
        "input_tokens": llm_result.get("input_tokens", 0),
        "output_tokens": llm_result.get("output_tokens", 0),
        "raw": llm_result.get("raw", ""),
    }
