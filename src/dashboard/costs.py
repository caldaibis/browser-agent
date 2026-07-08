"""LLM spend: per-run usage and weekly rollups.

Per-run cost can come from two places, in order of fidelity:
1. `logs/trajectories/{stem}.jsonl` — structured `turn_usage` events written
   by browser_agent (exact prompt/completion/cache split per turn).
2. the transcript text — the older regex parser in data.parse_token_usage,
   used when a run predates trajectories.

Self-improvement runs log their own `estimated_cost_usd=` line in
`logs/self_improvement/<ts>.log`; `spend_rollup` folds those in so the
overview's weekly spend reflects both the apply agent and the SI agent.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from ..config import LOG_DIR
from ..llm_pricing import DEFAULT_APPLY_MODEL
from . import cache, data

TRAJECTORY_DIR = LOG_DIR / "trajectories"
SI_LOG_DIR = LOG_DIR / "self_improvement"

_SI_COST_RE = re.compile(r"estimated_cost_usd=([0-9]*\.?[0-9]+)")


def usage_from_trajectory(stem: str) -> data.TokenUsage | None:
    """Sum a run's `turn_usage` trajectory events into a TokenUsage."""
    if not stem:
        return None
    path = TRAJECTORY_DIR / f"{stem}.jsonl"
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return None
    return _usage_from_trajectory_file(str(path), mtime_ns)


def _num(value) -> int | None:
    """Token counts in older trajectory files were redacted to '***' (a since-
    fixed over-broad key match). Treat any non-numeric value as unknown."""
    try:
        if value is None or value == "None":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=512)
def _usage_from_trajectory_file(path: str, mtime_ns: int) -> data.TokenUsage | None:
    model = ""
    input_tokens = output_tokens = total_tokens = 0
    reasoning = cache_hit = cache_miss = 0
    saw_real = False
    for rec in cache.jsonl_records(Path(path)):
        payload = rec.get("payload") or {}
        if rec.get("event") == "run_start" and payload.get("model"):
            model = str(payload["model"])
        if rec.get("event") != "turn_usage":
            continue
        completion = _num(payload.get("completion_tokens"))
        if completion is None:
            # Redacted/unusable turn counts -> let the caller fall back to the
            # transcript parser instead of reporting a bogus 0-token run.
            continue
        saw_real = True
        input_tokens += _num(payload.get("prompt_tokens")) or 0
        output_tokens += completion
        total_tokens += _num(payload.get("total_tokens")) or 0
        reasoning += _num(payload.get("reasoning_tokens")) or 0
        cache_hit += _num(payload.get("cache_hit_tokens")) or 0
        cache_miss += _num(payload.get("cache_miss_tokens")) or 0
    if not saw_real:
        return None
    model = model or DEFAULT_APPLY_MODEL
    cost, partial = data._estimate_cost(
        model=model, input_tokens=input_tokens, output_tokens=output_tokens,
        cache_hit_tokens=cache_hit, cache_miss_tokens=cache_miss or None,
    )
    return data.TokenUsage(
        model=model, input_tokens=input_tokens, output_tokens=output_tokens,
        total_tokens=total_tokens or None, reasoning_tokens=reasoning,
        cache_hit_tokens=cache_hit, cache_miss_tokens=cache_miss,
        estimated_cost_usd=cost, cost_is_partial=partial,
    )


def usage_for_submission(sub: data.Submission) -> data.TokenUsage | None:
    """Trajectory-first, transcript-fallback usage for one submission."""
    return usage_from_trajectory(sub.transcript_stem) or data.token_usage_for_submission(sub)


def _si_run_costs(cutoff: datetime) -> float:
    total = 0.0
    try:
        paths = list(SI_LOG_DIR.glob("*.log"))
    except OSError:
        return 0.0
    for path in paths:
        try:
            if datetime.fromtimestamp(path.stat().st_mtime) < cutoff:
                continue
        except OSError:
            continue
        total += _si_log_cost(str(path), _safe_mtime_ns(path))
    return total


def _safe_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def si_log_cost(path: Path | str) -> float:
    """Estimated USD cost of one self-improvement run from its per-run log."""
    p = Path(path)
    return _si_log_cost(str(p), _safe_mtime_ns(p))


@lru_cache(maxsize=512)
def _si_log_cost(path: str, mtime_ns: int) -> float:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0.0
    # Sum every phase's estimated_cost_usd line (diagnosis + patch).
    return sum(float(m) for m in _SI_COST_RE.findall(text))


def spend_rollup(days: int = 7) -> dict:
    """Apply-agent + SI-agent spend over the window, plus per-day and
    cost-per-submission. Uses each submission's best-available usage."""
    def _build() -> dict:
        now = datetime.now()
        cutoff = now - timedelta(days=days)
        apply_usd = 0.0
        submitted = 0
        per_day: dict[str, float] = {}
        for s in data.load_submissions():
            w = s.when
            if w is None or w < cutoff:
                continue
            u = usage_for_submission(s)
            c = (u.estimated_cost_usd or 0.0) if u else 0.0
            apply_usd += c
            per_day[w.strftime("%Y-%m-%d")] = per_day.get(w.strftime("%Y-%m-%d"), 0.0) + c
            if s.status in ("submitted", "applied"):
                submitted += 1
        si_usd = _si_run_costs(cutoff)
        return {
            "days": days,
            "apply_usd": round(apply_usd, 4),
            "si_usd": round(si_usd, 4),
            "total_usd": round(apply_usd + si_usd, 4),
            "submitted": submitted,
            "cost_per_submission": round(apply_usd / submitted, 4) if submitted else None,
            "per_day": {k: round(v, 4) for k, v in sorted(per_day.items())},
        }
    return cache.memo(f"spend_rollup:{days}", 30.0, _build)
