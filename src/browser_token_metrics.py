"""Aggregate apply-session token usage by browser backend and model."""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any


TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "reasoning_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)


def _number(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return 0


def _percentile(values: list[int], percentile: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def _read_session(path: Path) -> dict | None:
    start: dict = {}
    final: dict = {}
    usage: list[dict] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if rec.get("event") == "run_start":
                start = payload
            elif rec.get("event") == "turn_usage":
                usage.append(payload)
            elif rec.get("event") == "final":
                final = payload
    except (OSError, json.JSONDecodeError):
        return None
    if not usage:
        return None
    totals = {
        field: sum(_number(turn.get(field)) for turn in usage)
        for field in TOKEN_FIELDS
    }
    backend = str(start.get("browser_backend") or "playwright")
    version = str(start.get("browser_backend_version") or "legacy-unrecorded")
    model = str(start.get("model") or "unknown")
    return {
        "cohort": f"{backend}@{version}|{model}",
        "backend": backend,
        "backend_version": version,
        "model": model,
        "outcome": str(final.get("outcome") or "unknown"),
        "turns": len(usage),
        "token_usage_observed": totals["total_tokens"] > 0,
        **totals,
    }


def _distribution(values: list[int]) -> dict:
    return {
        "mean": round(statistics.fmean(values), 2),
        "median": round(statistics.median(values), 2),
        "p90": _percentile(values, 0.9),
        "min": min(values),
        "max": max(values),
    }


def build_report(directory: Path, minimum_sample_size: int = 10) -> dict:
    sessions = [
        session
        for path in sorted(directory.glob("*.jsonl"))
        if (session := _read_session(path)) is not None
    ]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for session in sessions:
        grouped[session["cohort"]].append(session)

    cohorts = {}
    for name, rows in sorted(grouped.items()):
        outcomes: dict[str, int] = defaultdict(int)
        for row in rows:
            outcomes[row["outcome"]] += 1
        observed = [row for row in rows if row["token_usage_observed"]]
        submitted = [row for row in observed if row["outcome"] == "submitted"]
        non_yielded = [row for row in observed if row["outcome"] != "yielded"]
        outcome_tokens = {
            outcome: _distribution([
                row["total_tokens"] for row in observed if row["outcome"] == outcome
            ])
            for outcome in sorted(outcomes)
            if any(row["outcome"] == outcome for row in observed)
        }
        cohorts[name] = {
            "backend": rows[0]["backend"],
            "backend_version": rows[0]["backend_version"],
            "model": rows[0]["model"],
            "sessions": len(rows),
            "token_observed_sessions": len(observed),
            "token_missing_sessions": len(rows) - len(observed),
            "minimum_sample_reached": len(rows) >= minimum_sample_size,
            "outcomes": dict(sorted(outcomes.items())),
            "turns_per_session": _distribution([row["turns"] for row in rows]),
            **{
                f"{field}_per_session": (
                    _distribution([row[field] for row in observed]) if observed else None
                )
                for field in TOKEN_FIELDS
            },
            "total_tokens_by_outcome": outcome_tokens,
            "non_yielded_observed_sessions": len(non_yielded),
            "total_tokens_per_non_yielded_session": (
                _distribution([row["total_tokens"] for row in non_yielded])
                if non_yielded else None
            ),
            "submitted_observed_sessions": len(submitted),
            "total_tokens_per_submitted_session": (
                _distribution([row["total_tokens"] for row in submitted])
                if submitted else None
            ),
        }

    ready = [name for name, cohort in cohorts.items() if cohort["minimum_sample_reached"]]
    return {
        "schema": "browser-session-token-metrics-v1",
        "source": str(directory),
        "definition": (
            "A session is one trajectory file with at least one turn_usage event. "
            "Session tokens are sums of provider-reported per-turn usage; sessions "
            "with no positive total are counted but excluded from token distributions."
        ),
        "legacy_inference": (
            "A run_start without browser_backend is classified as "
            "playwright@legacy-unrecorded."
        ),
        "minimum_sample_size": minimum_sample_size,
        "sessions": len(sessions),
        "comparison_ready": len(ready) >= 2,
        "comparison_ready_cohorts": ready,
        "cohorts": cohorts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", nargs="?", type=Path, default=Path("logs/trajectories"))
    parser.add_argument("--minimum-sample-size", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = build_report(args.directory, max(1, args.minimum_sample_size))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
