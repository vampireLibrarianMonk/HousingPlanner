"""PDF extraction helpers for HOA document vetting."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Dict, Callable, Optional, Tuple
import uuid
import time

import boto3


@dataclass
class DocumentPage:
    page_number: int
    text: str


@dataclass
class DocumentExtraction:
    document_name: str
    pages: List[DocumentPage]


ProgressCallback = Callable[[str, int], None]


def start_textract_job(
    file_bytes: bytes,
    document_name: str,
    *,
    region_name: str | None = None,
    bucket_name: str | None = None,
) -> Tuple[str, str]:
    """Upload to S3 and start Textract job. Returns (job_id, s3_key)."""
    if not bucket_name:
        raise ValueError("bucket_name is required for Textract PDF processing")

    client = boto3.client("textract", region_name=region_name)
    s3 = boto3.client("s3", region_name=region_name)
    key = f"hoa-uploads/{uuid.uuid4()}-{document_name}"

    s3.put_object(Bucket=bucket_name, Key=key, Body=file_bytes)
    response = client.start_document_text_detection(
        DocumentLocation={"S3Object": {"Bucket": bucket_name, "Name": key}},
    )
    return response["JobId"], key


def poll_textract_job(
    job_id: str,
    *,
    region_name: str | None = None,
    poll_delay_seconds: float = 1.0,
    max_polls: int = 40,
    on_progress: Optional[ProgressCallback] = None,
) -> List[Dict]:
    """Poll Textract job. Returns blocks when complete, raises on timeout or failure."""
    client = boto3.client("textract", region_name=region_name)

    blocks: List[Dict] = []
    next_token = None
    last_pages = 0
    for _ in range(max_polls):
        if next_token:
            result = client.get_document_text_detection(JobId=job_id, NextToken=next_token)
        else:
            result = client.get_document_text_detection(JobId=job_id)

        status = result.get("JobStatus", "UNKNOWN")
        if status == "FAILED":
            raise RuntimeError("Textract text detection failed")

        if status == "SUCCEEDED":
            blocks.extend(result.get("Blocks", []))
            next_token = result.get("NextToken")
            if on_progress:
                pages_found = _estimate_pages(blocks)
                if pages_found != last_pages:
                    last_pages = pages_found
                on_progress(status, last_pages)
            if not next_token:
                return blocks
        else:
            if on_progress:
                pages_found = _estimate_pages(blocks)
                if pages_found != last_pages:
                    last_pages = pages_found
                on_progress(status, last_pages)
            time.sleep(poll_delay_seconds)
            continue

    raise TimeoutError("Textract job did not complete in time")


def cleanup_textract_job(
    s3_key: str,
    *,
    region_name: str | None = None,
    bucket_name: str | None = None,
) -> None:
    if not bucket_name:
        return
    s3 = boto3.client("s3", region_name=region_name)
    s3.delete_object(Bucket=bucket_name, Key=s3_key)


def blocks_to_extraction(blocks: List[Dict], document_name: str) -> DocumentExtraction:
    page_map: Dict[int, List[str]] = {}
    for block in blocks:
        if block.get("BlockType") == "LINE":
            page = block.get("Page", 1)
            page_map.setdefault(page, []).append(block.get("Text", ""))

    pages = [
        DocumentPage(page_number=page, text="\n".join(lines).strip())
        for page, lines in sorted(page_map.items())
    ]
    return DocumentExtraction(document_name=document_name, pages=pages)


def extract_text_with_textract(
    file_bytes: bytes,
    document_name: str,
    *,
    region_name: str | None = None,
    bucket_name: str | None = None,
    poll_delay_seconds: float = 1.0,
    max_polls: int = 40,
    on_progress: Optional[ProgressCallback] = None,
) -> DocumentExtraction:
    """Extract PDF text using AWS Textract (async job via S3)."""
    job_id, s3_key = start_textract_job(
        file_bytes,
        document_name,
        region_name=region_name,
        bucket_name=bucket_name,
    )
    try:
        blocks = poll_textract_job(
            job_id,
            region_name=region_name,
            poll_delay_seconds=poll_delay_seconds,
            max_polls=max_polls,
            on_progress=on_progress,
        )
    finally:
        cleanup_textract_job(s3_key, region_name=region_name, bucket_name=bucket_name)

    return blocks_to_extraction(blocks, document_name)


def _estimate_pages(blocks: List[Dict]) -> int:
    pages = {block.get("Page") for block in blocks if block.get("BlockType") == "LINE"}
    return len([p for p in pages if p])


def build_page_context(extraction: DocumentExtraction) -> str:
    """Build a single string with page markers for LLM context."""
    sections = []
    for page in extraction.pages:
        sections.append(f"\n\n--- Page {page.page_number} ---\n{page.text}")
    return "".join(sections).strip()
