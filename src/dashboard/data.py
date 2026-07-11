"""Read-only data layer for the dashboard.

Parses the agent's existing log/state files (no DB) and exposes submissions,
stats, redacted transcripts, and live health. SECURITY: transcripts may contain
credentials (the apply prompt embeds site passwords); everything served goes
through redact(), and *.prompt.txt is never read here.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from .. import eventlog
from ..config import LOG_DIR, PROJECT_ROOT
from ..llm_pricing import pricing_table
from . import cache

MAIL_SUMMARY = LOG_DIR / "mail_summary.jsonl"
TRANSCRIPTS_DIR = LOG_DIR / "transcripts"
SCREENSHOTS_DIR = LOG_DIR / "screenshots"
CREDS_FILE = PROJECT_ROOT / "state" / "sources_credentials.json"

# Outcomes that represent a real apply attempt (for success-rate maths).
ATTEMPT_STATUSES = {
    "submitted", "applied", "already_applied", "not_available", "not_eligible",
    "login_required", "blocked", "timeout", "incomplete", "error",
}
def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return (s or "listing")[:50]


# --------------------------------------------------------------------------- #
# Submissions
# --------------------------------------------------------------------------- #
@dataclass
class Submission:
    id: int
    ts: str
    status: str
    source: str
    address: str
    source_url: str
    stekkies_url: str
    seconds: float | None
    message: str
    trigger: str = ""
    detected_by: str = ""
    msg_id: str = ""
    msg_received_ts: str = ""
    detected_ts: str = ""
    token_usage: TokenUsage | None = None

    @property
    def when(self) -> datetime | None:
        return parse_ts(self.ts)

    @property
    def origin(self) -> str:
        if self.trigger:
            return self.trigger
        if self.msg_id or self.stekkies_url:
            return "stekkies_mail"
        return "unknown"

    @property
    def origin_label(self) -> str:
        return {
            "poller": "Poller",
            "stekkies_mail": "Stekkies mail",
            "huurwoningen_mail": "Huurwoningen mail",
            "manual": "Manual",
        }.get(self.origin, self.origin.replace("_", " ").title() or "Unknown")

    @property
    def detected_by_label(self) -> str:
        if self.detected_by:
            return self.detected_by
        if self.origin == "poller":
            return self.source
        return ""

    @property
    def event_time(self) -> datetime | None:
        return parse_ts(self.detected_ts) or parse_ts(self.msg_received_ts) or self.when

    @property
    def permalink(self) -> str:
        """Stable per-record id for URLs. Derived from content (timestamp +
        hash of source_url/msg_id), so it survives log edits/rotation that
        would shift the line-index `id`. `/submission/<int>` still resolves
        the legacy line index for old bookmarks/push links."""
        ts_compact = re.sub(r"[^0-9]", "", self.ts)[:14] or "0"
        seed = self.source_url or self.stekkies_url or self.msg_id or f"line{self.id}"
        return f"{ts_compact}-{hashlib.sha1(seed.encode()).hexdigest()[:8]}"

    @property
    def transcript_stem(self) -> str:
        """Filename stem shared by this run's transcript and trajectory:
        `{ts}_{slug(source-address)}` (see apply.apply / browser_agent)."""
        p = find_transcript(self)
        return p.stem if p else ""


@dataclass
class TokenUsage:
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int = 0
    total_tokens: int | None = None
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int | None = None
    estimated_cost_usd: float | None = None
    cost_is_partial: bool = False

    @property
    def display_tokens(self) -> str:
        if self.total_tokens is not None:
            return f"{format_count(self.total_tokens)} tok"
        if self.input_tokens is not None:
            return f"{format_count(self.input_tokens + self.output_tokens)} tok"
        return f"{format_count(self.output_tokens)} out"

    @property
    def display_cost(self) -> str:
        if self.estimated_cost_usd is None:
            return "—"
        prefix = "≥" if self.cost_is_partial else ""
        return f"{prefix}{format_usd(self.estimated_cost_usd)}"

    @property
    def display_summary(self) -> str:
        if self.estimated_cost_usd is None:
            return self.display_tokens
        return f"{self.display_cost} · {self.display_tokens}"


def parse_ts(value: str | None) -> datetime | None:
    # Delegates to eventlog.parse_ts: old records are naive local time, new
    # ones aware UTC; both normalize to naive local so age math stays right.
    return eventlog.parse_ts(value)


def format_age(seconds: float | None) -> str:
    """'2m ago' / '1h 5m ago' style, for "when did this last happen"."""
    if seconds is None:
        return "never"
    seconds = max(0, seconds)
    mins = int(seconds // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h {mins % 60}m ago"
    return f"{hours // 24}d {hours % 24}h ago"


def format_duration(seconds: float | None) -> str:
    """A plain elapsed-time duration ('3m 20s', '1h 5m'), distinct from
    format_age's '... ago' framing."""
    if seconds is None:
        return "—"
    seconds = int(max(0, seconds))
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h {mins}m"


def format_count(value: int | None) -> str:
    if value is None:
        return "—"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def format_usd(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 0.01:
        return f"${value:.4f}"
    return f"${value:.2f}"


_USAGE_FIELD_RE = re.compile(r"([a-z_]+)=([0-9]+|None)")
_MODEL_RE = re.compile(r"\[agent\]\s+model=([^\s]+)")


def _int_or_none(value: str | None) -> int | None:
    if not value or value == "None":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _estimate_cost(
    *,
    model: str,
    input_tokens: int | None,
    output_tokens: int,
    cache_hit_tokens: int,
    cache_miss_tokens: int | None,
) -> tuple[float | None, bool]:
    prices = pricing_table().get((model or "").lower())
    if not prices:
        return None, False
    output_cost = output_tokens * prices["output"] / 1_000_000
    if input_tokens is None:
        return output_cost if output_tokens else None, bool(output_tokens)

    if cache_hit_tokens and cache_miss_tokens is None:
        cache_miss_tokens = max(input_tokens - cache_hit_tokens, 0)
    if cache_hit_tokens or cache_miss_tokens is not None:
        miss = cache_miss_tokens if cache_miss_tokens is not None else input_tokens
        input_cost = (
            cache_hit_tokens * prices["cached_input"] / 1_000_000
            + miss * prices["input"] / 1_000_000
        )
    else:
        input_cost = input_tokens * prices["input"] / 1_000_000
    return input_cost + output_cost, False


def parse_token_usage(text: str) -> TokenUsage | None:
    """Parse per-turn usage lines from an apply transcript.

    Older transcripts only logged completion/reasoning tokens. For those rows
    we still show output tokens and a lower-bound output cost.
    """
    if not text:
        return None
    model = ""
    input_tokens: int | None = None
    output_tokens = 0
    total_tokens: int | None = None
    reasoning_tokens = 0
    cache_hit_tokens = 0
    cache_miss_tokens: int | None = None
    saw_usage = False
    saw_prompt = False
    for line in text.splitlines():
        model_match = _MODEL_RE.search(line)
        if model_match:
            model = model_match.group(1)
        if "completion_tokens=" not in line:
            continue
        fields = {k: _int_or_none(v) for k, v in _USAGE_FIELD_RE.findall(line)}
        if not fields:
            continue
        saw_usage = True
        prompt = fields.get("prompt_tokens") or fields.get("input_tokens")
        if prompt is not None:
            saw_prompt = True
            input_tokens = (input_tokens or 0) + prompt
        completion = fields.get("completion_tokens") or fields.get("output_tokens")
        if completion is not None:
            output_tokens += completion
        total = fields.get("total_tokens")
        if total is not None:
            total_tokens = (total_tokens or 0) + total
        reasoning = fields.get("reasoning_tokens")
        if reasoning is not None:
            reasoning_tokens += reasoning
        hit = (
            fields.get("cache_hit_tokens")
            or fields.get("prompt_cache_hit_tokens")
            or fields.get("cached_tokens")
        )
        if hit is not None:
            cache_hit_tokens += hit
        miss = fields.get("cache_miss_tokens") or fields.get("prompt_cache_miss_tokens")
        if miss is not None:
            cache_miss_tokens = (cache_miss_tokens or 0) + miss
    if not saw_usage:
        return None
    if not saw_prompt:
        input_tokens = None
        total_tokens = None
    cost, partial = _estimate_cost(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
    )
    return TokenUsage(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_hit_tokens=cache_hit_tokens,
        cache_miss_tokens=cache_miss_tokens,
        estimated_cost_usd=cost,
        cost_is_partial=partial,
    )


@lru_cache(maxsize=512)
def _parse_token_usage_file(path: str, mtime_ns: int) -> TokenUsage | None:
    try:
        return parse_token_usage(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def token_usage_for_submission(sub: Submission) -> TokenUsage | None:
    p = find_transcript(sub)
    if not p:
        return None
    try:
        return _parse_token_usage_file(str(p), p.stat().st_mtime_ns)
    except Exception:
        return None


# Derived views are memoized for a few seconds so the several internal
# callers within one request (and the 30s/45s htmx polls) collapse onto one
# computation. jsonl_records() underneath is already an incremental tail read.
_CACHE_TTL = 5.0


def _build_submissions() -> list[Submission]:
    out: list[Submission] = []
    for i, r in enumerate(cache.jsonl_records(MAIL_SUMMARY)):
        sub = Submission(
            id=i,
            ts=r.get("ts", ""),
            status=r.get("status", "unknown"),
            source=r.get("source") or "",
            address=r.get("address") or "",
            source_url=r.get("source_url") or "",
            stekkies_url=r.get("stekkies_url") or "",
            seconds=r.get("seconds"),
            message=r.get("message") or "",
            trigger=r.get("trigger") or "",
            detected_by=r.get("detected_by") or "",
            msg_id=r.get("msg_id") or "",
            msg_received_ts=r.get("msg_received_ts") or "",
            detected_ts=r.get("detected_ts") or "",
        )
        sub.token_usage = token_usage_for_submission(sub)
        out.append(sub)
    return out


def load_submissions() -> list[Submission]:
    return cache.memo("submissions", _CACHE_TTL, _build_submissions)


def _submission_index() -> dict[str, Submission]:
    def _build() -> dict[str, Submission]:
        idx: dict[str, Submission] = {}
        for s in load_submissions():
            idx[str(s.id)] = s          # legacy line-index permalinks
            idx[s.permalink] = s        # stable content-hash permalinks
        return idx
    return cache.memo("submission_index", _CACHE_TTL, _build)


def get_submission(key: str | int) -> Submission | None:
    """Look up by stable permalink or legacy line-index id (as str or int)."""
    return _submission_index().get(str(key))


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #
def compute_stats(subs: list[Submission]) -> dict:
    by_status = Counter(s.status for s in subs)
    attempts = [s for s in subs if s.status in ATTEMPT_STATUSES]
    submitted = by_status.get("submitted", 0) + by_status.get("applied", 0)
    by_source = Counter(s.source for s in attempts if s.source)
    by_origin = Counter(s.origin for s in attempts)
    per_day: dict[str, int] = defaultdict(int)
    for s in attempts:
        w = s.when
        if w:
            per_day[w.strftime("%Y-%m-%d")] += 1
    durations = [s.seconds for s in attempts if isinstance(s.seconds, (int, float))]
    return {
        "total_handled": len(subs),
        "attempts": len(attempts),
        "submitted": submitted,
        "success_rate": round(100 * submitted / len(attempts), 1) if attempts else 0.0,
        "by_status": dict(by_status.most_common()),
        "by_source": dict(by_source.most_common()),
        "by_origin": dict(by_origin.most_common()),
        "per_day": dict(sorted(per_day.items())),
        "avg_seconds": round(sum(durations) / len(durations), 1) if durations else 0.0,
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def mission_kpis(subs: list[Submission], spend: dict) -> dict:
    """The "is the mission on track" tiles for the overview: submissions,
    success rate, detection→submitted latency, spend."""
    now = datetime.now()
    week_cutoff = now - timedelta(days=7)
    attempts = [s for s in subs if s.status in ATTEMPT_STATUSES]
    submitted = [s for s in subs if s.status in ("submitted", "applied")]
    submitted_week = sum(1 for s in submitted if (s.when or datetime.min) >= week_cutoff)

    # detection → submitted latency: from when we first saw the listing
    # (mail received_ts) to the submitted record time.
    latencies: list[float] = []
    for s in submitted:
        start = s.event_time
        end = s.when
        if start and end and end >= start:
            latencies.append((end - start).total_seconds())
    median_latency = _median(latencies)

    return {
        "submitted_total": len(submitted),
        "submitted_week": submitted_week,
        "attempts_total": len(attempts),
        "success_rate": round(100 * len(submitted) / len(attempts), 1) if attempts else 0.0,
        "median_latency_s": median_latency,
        "spend_week_usd": spend.get("total_usd"),
        "cost_per_submission": spend.get("cost_per_submission"),
    }


def warm_dashboard_caches() -> None:
    """Populate expensive dashboard caches off the request path.

    Fail-open so a warmup failure never takes the dashboard down.
    """
    try:
        load_submissions()
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Transcripts (redacted)
# --------------------------------------------------------------------------- #
# The implementation moved to src/redaction.py so non-dashboard writers
# (eventlog, notify, trajectories) share it; re-exported for existing callers.
from ..redaction import redact  # noqa: E402  (section-local re-export)


def find_transcript(sub: Submission) -> Path | None:
    if not TRANSCRIPTS_DIR.exists():
        return None
    slug = _slug(f"{sub.source}-{sub.address}")
    candidates = sorted(TRANSCRIPTS_DIR.glob(f"*_{slug}.log"))
    if not candidates:
        return None
    target = sub.when
    if target is None:
        return candidates[-1]

    def _ts(p: Path) -> datetime | None:
        try:
            return datetime.strptime(p.name[:15], "%Y%m%d_%H%M%S")
        except Exception:
            return None

    scored = [(p, _ts(p)) for p in candidates]
    scored = [(p, t) for p, t in scored if t is not None]
    if not scored:
        return candidates[-1]
    # closest transcript timestamp to the record timestamp
    return min(scored, key=lambda pt: abs((pt[1] - target).total_seconds()))[0]


def load_transcript(sub: Submission) -> str | None:
    p = find_transcript(sub)
    if not p:
        return None
    try:
        return redact(p.read_text(encoding="utf-8"))
    except Exception:
        return None

