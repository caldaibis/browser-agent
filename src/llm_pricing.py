"""Single source of truth for LLM token pricing.

Both the dashboard's cost estimator (`src/dashboard/costs.py`,
`src/dashboard/data.py`) and the self-improvement agent's cost estimator
(`src/self_improvement_agent.py`) used to carry their own copies of
deepseek-v4-pro's rates in different representations (per-1M vs per-token),
which could silently drift apart. They now both import from here.

Prices are USD per 1,000,000 tokens, matching the DeepSeek pricing page
(checked 2026-07-01). Override without a code change via
`LLM_MODEL_PRICES_JSON` (a `{model: {input, cached_input, output}}` map) or
the global `LLM_{INPUT,CACHED_INPUT,OUTPUT}_USD_PER_1M` env vars.
"""
from __future__ import annotations

import json
import os

# USD per 1,000,000 tokens.
DEFAULT_MODEL_PRICES: dict[str, dict[str, float]] = {
    "deepseek-v4-pro": {"input": 0.435, "cached_input": 0.003625, "output": 0.87},
    "deepseek-v4-flash": {"input": 0.14, "cached_input": 0.0028, "output": 0.28},
    "deepseek-chat": {"input": 0.14, "cached_input": 0.0028, "output": 0.28},
    "deepseek-reasoner": {"input": 0.14, "cached_input": 0.0028, "output": 0.28},
}

DEFAULT_APPLY_MODEL = "deepseek-v4-pro"


def pricing_table() -> dict[str, dict[str, float]]:
    """The per-1M price table with env overrides applied."""
    prices = {k: dict(v) for k, v in DEFAULT_MODEL_PRICES.items()}
    raw = os.environ.get("LLM_MODEL_PRICES_JSON", "")
    if raw:
        try:
            for model, vals in json.loads(raw).items():
                prices[str(model).lower()] = {
                    "input": float(vals["input"]),
                    "cached_input": float(vals.get("cached_input", vals["input"])),
                    "output": float(vals["output"]),
                }
        except Exception:
            pass
    global_input = os.environ.get("LLM_INPUT_USD_PER_1M")
    global_cached = os.environ.get("LLM_CACHED_INPUT_USD_PER_1M")
    global_output = os.environ.get("LLM_OUTPUT_USD_PER_1M")
    if global_input and global_output:
        try:
            for vals in prices.values():
                vals["input"] = float(global_input)
                vals["cached_input"] = float(global_cached or global_input)
                vals["output"] = float(global_output)
        except ValueError:
            pass
    return prices


def rates_per_token(model: str) -> dict[str, float]:
    """Per-*token* rates keyed input_miss/input_hit/output for a model.

    The shape the self-improvement agent's `_estimate_deepseek_cost_usd`
    expects. Falls back to deepseek-v4-pro when the model is unknown, since
    that estimator is only ever used for the deepseek-backed SI runs.
    """
    table = pricing_table()
    p = table.get((model or "").lower()) or table[DEFAULT_APPLY_MODEL]
    return {
        "input_miss": p["input"] / 1_000_000,
        "input_hit": p["cached_input"] / 1_000_000,
        "output": p["output"] / 1_000_000,
    }
