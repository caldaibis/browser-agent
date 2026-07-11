"""Where the pipeline leaks: mail-trigger funnel + failure/incident Pareto.

The apply outcomes land in mail_summary.jsonl. Joining them by trigger shows
attempted vs submitted per mail source, so a source that surfaces listings
but never converts stands out. Fail-open throughout.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta

from . import data

ATTEMPT_STATUSES = data.ATTEMPT_STATUSES


def mail_funnel(days: int = 7) -> list[dict]:
    """Attempt/submit counts for the mail-triggered paths, by trigger."""
    cutoff = datetime.now() - timedelta(days=days)
    stages: dict[str, dict] = defaultdict(lambda: {"attempted": 0, "submitted": 0})
    for s in data.load_submissions():
        w = s.when
        if w is None or w < cutoff:
            continue
        st = stages[s.origin_label]
        if s.status in ATTEMPT_STATUSES:
            st["attempted"] += 1
        if s.status in ("submitted", "applied"):
            st["submitted"] += 1
    return [{"trigger": k, **v} for k, v in sorted(stages.items())]


def failure_pareto(days: int = 7) -> list[tuple[str, int]]:
    """Non-submitted apply outcomes, most common first (all triggers)."""
    cutoff = datetime.now() - timedelta(days=days)
    counts: Counter = Counter()
    for s in data.load_submissions():
        w = s.when
        if w is None or w < cutoff:
            continue
        if s.status in ATTEMPT_STATUSES and s.status not in ("submitted", "applied"):
            counts[s.status] += 1
    return counts.most_common()


def incident_pareto(days: int = 30) -> list[tuple[str, int]]:
    from ..incident_store import incident_summary
    try:
        rows = incident_summary(days=days)
    except Exception:
        return []
    return sorted(((r["fingerprint"], r["occurrences"]) for r in rows),
                  key=lambda kv: -kv[1])
