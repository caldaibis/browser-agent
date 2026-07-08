"""Where the pipeline leaks: a per-source funnel + failure Pareto.

The poller emits per-URL decision events to logs/poller.jsonl
(`filtered_out`, `judged_out`, `qualified`, `apply_start`, `apply_done`) and
per-poll counts (`polled {site,total,new}`). The apply outcomes land in
mail_summary.jsonl. Joining them per source domain shows, for each site:

    seen → filtered_out → judged_out → qualified → attempted → submitted

so a site that surfaces listings but never converts (qualified > 0,
submitted == 0) stands out — that's a filter that's too loose, a judge
that's too strict, or a site the apply agent can't actually complete.

`filtered_out`/`judged_out`/`qualified`/`apply_*` events carry only a URL, so
everything is keyed on the URL's domain. Fail-open throughout.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlparse

from . import cache, data

ATTEMPT_STATUSES = data.ATTEMPT_STATUSES


def _domain(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _within(ts: str, cutoff: datetime) -> bool:
    dt = data.parse_ts(ts)
    return dt is not None and dt >= cutoff


def funnel_by_domain(days: int = 7) -> list[dict]:
    def _build() -> list[dict]:
        cutoff = datetime.now() - timedelta(days=days)
        stages: dict[str, dict] = defaultdict(lambda: {
            "seen": 0, "filtered": 0, "judged": 0, "qualified": 0,
            "attempted": 0, "submitted": 0,
        })

        for rec in cache.jsonl_records(data.POLL_LOG):
            if not _within(rec.get("ts", ""), cutoff):
                continue
            event = rec.get("event")
            if event == "polled":
                site = rec.get("site")
                if site:
                    stages[site]["seen"] += int(rec.get("new") or 0)
            elif event == "filtered_out":
                stages[_domain(rec.get("url", ""))]["filtered"] += 1
            elif event == "judged_out":
                stages[_domain(rec.get("url", ""))]["judged"] += 1
            elif event == "qualified":
                stages[_domain(rec.get("url", ""))]["qualified"] += 1
            elif event == "apply_start":
                stages[_domain(rec.get("url", ""))]["attempted"] += 1

        # Attempts/submissions from mail_summary (poller-origin), joined by
        # domain — the poller path's true outcomes live here, not in poller.jsonl.
        for s in data.load_submissions():
            if s.origin != "poller":
                continue
            w = s.when
            if w is None or w < cutoff:
                continue
            dom = _domain(s.source_url) or (s.detected_by or s.source)
            if s.status in ATTEMPT_STATUSES:
                stages[dom]["attempted_mail"] = stages[dom].get("attempted_mail", 0) + 1
            if s.status in ("submitted", "applied"):
                stages[dom]["submitted"] += 1

        rows = []
        for dom, st in stages.items():
            if not dom:
                continue
            attempted = max(st["attempted"], st.get("attempted_mail", 0))
            rows.append({
                "domain": dom,
                "seen": st["seen"],
                "filtered": st["filtered"],
                "judged": st["judged"],
                "qualified": st["qualified"],
                "attempted": attempted,
                "submitted": st["submitted"],
                "leak": st["qualified"] > 0 and st["submitted"] == 0,
            })
        rows.sort(key=lambda r: (-(r["qualified"] + r["attempted"]), r["domain"]))
        return rows
    return cache.memo(f"funnel:{days}", 15.0, _build)


def mail_funnel(days: int = 7) -> list[dict]:
    """Attempt/submit counts for the mail-triggered paths, by trigger."""
    cutoff = datetime.now() - timedelta(days=days)
    stages: dict[str, dict] = defaultdict(lambda: {"attempted": 0, "submitted": 0})
    for s in data.load_submissions():
        if s.origin == "poller":
            continue
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


def reason_breakdown(days: int = 7, top: int = 12) -> dict[str, list[tuple[str, int]]]:
    """Most common filter/judge veto reasons — the knobs worth tuning."""
    cutoff = datetime.now() - timedelta(days=days)
    filtered: Counter = Counter()
    judged: Counter = Counter()
    for rec in cache.jsonl_records(data.POLL_LOG):
        if not _within(rec.get("ts", ""), cutoff):
            continue
        if rec.get("event") == "filtered_out":
            filtered[_short_reason(rec.get("reason", ""))] += 1
        elif rec.get("event") == "judged_out":
            judged[_short_reason(rec.get("reason", ""))] += 1
    return {"filtered": filtered.most_common(top), "judged": judged.most_common(top)}


def _short_reason(reason: str) -> str:
    reason = (reason or "").strip()
    # collapse numbers so "price 1850 > 1750" and "price 2000 > 1750" group.
    import re
    reason = re.sub(r"\d[\d.,]*", "N", reason)
    return reason[:80] or "(no reason)"
