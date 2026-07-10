"""Real (not SDK-reported) cost of a proxied DeepSeek run.

The SDK's own total_cost_usd is ~19.5x off for the proxied model (see
AGENTS.md gotchas); this estimate from raw usage tokens is the number to
trust and log."""
from __future__ import annotations

from .. import llm_pricing as _pricing

# deepseek-v4-pro per-token rates (USD). Single-sourced from src/llm_pricing.py
# (the same table the dashboard cost estimator uses) so the two can't drift;
# input_miss/input_hit/output = input/cached_input/output per token.
_DEEPSEEK_V4_PRO_RATES = _pricing.rates_per_token("deepseek-v4-pro")


def _estimate_deepseek_cost_usd(usage: dict) -> float:
    input_tokens = int((usage or {}).get("input_tokens") or 0)
    cache_read = int((usage or {}).get("cache_read_input_tokens") or 0)
    cache_write = int((usage or {}).get("cache_creation_input_tokens") or 0)
    output_tokens = int((usage or {}).get("output_tokens") or 0)
    return (
        input_tokens * _DEEPSEEK_V4_PRO_RATES["input_miss"]
        + cache_read * _DEEPSEEK_V4_PRO_RATES["input_hit"]
        + cache_write * _DEEPSEEK_V4_PRO_RATES["input_miss"]
        + output_tokens * _DEEPSEEK_V4_PRO_RATES["output"]
    )
