"""Pricing registry and inference profile mappings for Bedrock Claude models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List


PRICING_VERSION = "2026-01"


@dataclass(frozen=True)
class PricingProfile:
    model_id: str
    input_per_1m: float
    output_per_1m: float
    context: str


PRICING_REGISTRY: Dict[str, PricingProfile] = {
    "anthropic.claude-haiku-4-5": PricingProfile(
        model_id="anthropic.claude-haiku-4-5",
        input_per_1m=1.10,
        output_per_1m=5.50,
        context="standard",
    ),
    "anthropic.claude-sonnet-4-5": PricingProfile(
        model_id="anthropic.claude-sonnet-4-5",
        input_per_1m=3.30,
        output_per_1m=16.50,
        context="standard",
    ),
    "anthropic.claude-sonnet-4-5-long": PricingProfile(
        model_id="anthropic.claude-sonnet-4-5",
        input_per_1m=6.60,
        output_per_1m=24.75,
        context="long",
    ),
    "anthropic.claude-opus-4-5": PricingProfile(
        model_id="anthropic.claude-opus-4-5",
        input_per_1m=5.50,
        output_per_1m=27.50,
        context="standard",
    ),
    "anthropic.claude-opus-4-1": PricingProfile(
        model_id="anthropic.claude-opus-4-1",
        input_per_1m=15.00,
        output_per_1m=75.00,
        context="standard",
    ),
    "anthropic.claude-opus-4": PricingProfile(
        model_id="anthropic.claude-opus-4",
        input_per_1m=15.00,
        output_per_1m=75.00,
        context="standard",
    ),
    "anthropic.claude-sonnet-4": PricingProfile(
        model_id="anthropic.claude-sonnet-4",
        input_per_1m=3.00,
        output_per_1m=15.00,
        context="standard",
    ),
    "anthropic.claude-sonnet-4-long": PricingProfile(
        model_id="anthropic.claude-sonnet-4",
        input_per_1m=6.00,
        output_per_1m=22.50,
        context="long",
    ),
    "anthropic.claude-3-7-sonnet": PricingProfile(
        model_id="anthropic.claude-3-7-sonnet",
        input_per_1m=3.00,
        output_per_1m=15.00,
        context="standard",
    ),
    "anthropic.claude-3-5-sonnet": PricingProfile(
        model_id="anthropic.claude-3-5-sonnet",
        input_per_1m=3.00,
        output_per_1m=15.00,
        context="standard",
    ),
    "anthropic.claude-3-5-haiku": PricingProfile(
        model_id="anthropic.claude-3-5-haiku",
        input_per_1m=0.80,
        output_per_1m=4.00,
        context="standard",
    ),
    "anthropic.claude-3-5-sonnet-v2": PricingProfile(
        model_id="anthropic.claude-3-5-sonnet-v2",
        input_per_1m=3.00,
        output_per_1m=15.00,
        context="standard",
    ),
    "anthropic.claude-3-opus": PricingProfile(
        model_id="anthropic.claude-3-opus",
        input_per_1m=15.00,
        output_per_1m=75.00,
        context="standard",
    ),
    "anthropic.claude-3-haiku": PricingProfile(
        model_id="anthropic.claude-3-haiku",
        input_per_1m=0.25,
        output_per_1m=1.25,
        context="standard",
    ),
    "anthropic.claude-3-sonnet": PricingProfile(
        model_id="anthropic.claude-3-sonnet",
        input_per_1m=3.00,
        output_per_1m=15.00,
        context="standard",
    ),
    "anthropic.claude-2-1": PricingProfile(
        model_id="anthropic.claude-2-1",
        input_per_1m=8.00,
        output_per_1m=24.00,
        context="standard",
    ),
    "anthropic.claude-2": PricingProfile(
        model_id="anthropic.claude-2",
        input_per_1m=8.00,
        output_per_1m=24.00,
        context="standard",
    ),
    "anthropic.claude-instant": PricingProfile(
        model_id="anthropic.claude-instant",
        input_per_1m=0.80,
        output_per_1m=2.40,
        context="standard",
    ),
}


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
    pricing = PRICING_REGISTRY[model_key]
    return (
        input_tokens * pricing.input_per_1m
        + output_tokens * pricing.output_per_1m
    ) / 1_000_000