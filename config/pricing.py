"""Pricing registry and inference profile mappings for Bedrock Claude models."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List
import json


PRICING_PATH = Path(__file__).resolve().parents[1] / "pricing" / "static.json"


@dataclass(frozen=True)
class PricingProfile:
    model_id: str
    input_per_1m: float
    output_per_1m: float
    context: str


def _load_pricing_data() -> dict:
    if not PRICING_PATH.exists():
        raise FileNotFoundError(f"Pricing registry not found: {PRICING_PATH}")
    with PRICING_PATH.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def get_pricing_version() -> str:
    data = _load_pricing_data()
    metadata = data.get("metadata", {})
    return metadata.get("version", "unknown")


def get_llm_pricing_registry() -> Dict[str, PricingProfile]:
    data = _load_pricing_data()
    registry: Dict[str, PricingProfile] = {}
    for entry in data.get("llm_pricing", []):
        model_key = entry.get("model_key")
        if not model_key:
            continue
        registry[model_key] = PricingProfile(
            model_id=entry.get("model_id", model_key),
            input_per_1m=float(entry.get("input_per_1m", 0.0)),
            output_per_1m=float(entry.get("output_per_1m", 0.0)),
            context=entry.get("context", "standard"),
        )
    return registry


def get_api_pricing_entries() -> list[dict]:
    data = _load_pricing_data()
    return list(data.get("api_pricing", []))


@dataclass(frozen=True)
class InferenceProfile:
    profile_id: str
    name: str
    model_id: str


CLAUDE_INFERENCE_PROFILES: List[InferenceProfile] = [
    InferenceProfile(
        profile_id="us.anthropic.claude-3-sonnet-20240229-v1:0",
        name="US Claude 3 Sonnet",
        model_id="anthropic.claude-3-sonnet-20240229-v1:0",
    ),
    InferenceProfile(
        profile_id="us.anthropic.claude-3-opus-20240229-v1:0",
        name="US Claude 3 Opus",
        model_id="anthropic.claude-3-opus-20240229-v1:0",
    ),
    InferenceProfile(
        profile_id="us.anthropic.claude-3-haiku-20240307-v1:0",
        name="US Claude 3 Haiku",
        model_id="anthropic.claude-3-haiku-20240307-v1:0",
    ),
    InferenceProfile(
        profile_id="global.anthropic.claude-opus-4-5-20251101-v1:0",
        name="Global Claude Opus 4.5",
        model_id="anthropic.claude-opus-4-5-20251101-v1:0",
    ),
    InferenceProfile(
        profile_id="us.anthropic.claude-opus-4-1-20250805-v1:0",
        name="US Claude Opus 4.1",
        model_id="anthropic.claude-opus-4-1-20250805-v1:0",
    ),
    InferenceProfile(
        profile_id="global.anthropic.claude-sonnet-4-5-20250929-v1:0",
        name="Global Claude Sonnet 4.5",
        model_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
    ),
    InferenceProfile(
        profile_id="us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        name="US Claude Sonnet 4.5",
        model_id="anthropic.claude-sonnet-4-5-20250929-v1:0",
    ),
]


def estimate_request_cost(model_key: str, input_tokens: int, output_tokens: int) -> float:
    pricing = get_llm_pricing_registry()[model_key]
    return (
        input_tokens * pricing.input_per_1m
        + output_tokens * pricing.output_per_1m
    ) / 1_000_000