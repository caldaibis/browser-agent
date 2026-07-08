"""Read-only data layer for the dashboard.

Parses the agent's existing log/state files (no DB) and exposes submissions,
stats, redacted transcripts, and live health. SECURITY: transcripts may contain
credentials (the apply prompt embeds site passwords); everything served goes
through redact(), and *.prompt.txt is never read here.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from ..config import LOG_DIR, PROJECT_ROOT
from ..gmail_watch import recent_mail_events
from ..llm_pricing import pricing_table
from ..poller.dedup import canonical_url
from . import cache

MAIL_SUMMARY = LOG_DIR / "mail_summary.jsonl"
TRANSCRIPTS_DIR = LOG_DIR / "transcripts"
SCREENSHOTS_DIR = LOG_DIR / "screenshots"
CREDS_FILE = PROJECT_ROOT / "state" / "sources_credentials.json"
MAIL_EVENTS_CACHE = PROJECT_ROOT / "state" / "dashboard_mail_events.json"

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
    token_usage: "TokenUsage | None" = None

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
    def key(self) -> str:
        """Canonical listing key — used to group a listing's records together
        (race pairing). NOT a stable per-record id; several records share it."""
        return canonical_url(self.source_url) if self.source_url else ""

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


@dataclass
class MailEvent:
    provider: str
    msg_id: str
    received_ts: str
    subject: str
    source_url: str = ""
    stekkies_url: str = ""

    @property
    def when(self) -> datetime | None:
        return parse_ts(self.received_ts)

    @property
    def key(self) -> str:
        return canonical_url(self.source_url) if self.source_url else ""


@dataclass
class RaceInfo:
    id: int
    source_url: str
    source: str
    detected_by: str
    address: str
    status: str
    permalink: str = ""
    poller_ts: str = ""
    stekkies_ts: str = ""
    huurwoningen_ts: str = ""
    stekkies_lead_s: float | None = None
    huurwoningen_lead_s: float | None = None

    @property
    def poller_won_stekkies(self) -> bool:
        return self.stekkies_lead_s is not None and self.stekkies_lead_s > 0

    @property
    def poller_won_huurwoningen(self) -> bool:
        return self.huurwoningen_lead_s is not None and self.huurwoningen_lead_s > 0


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def format_delta(seconds: float | None) -> str:
    if seconds is None:
        return "no mail yet"
    ahead = seconds >= 0
    seconds = abs(seconds)
    mins = int(seconds // 60)
    secs = int(seconds % 60)
    if mins >= 60:
        text = f"{mins // 60}h {mins % 60}m"
    elif mins:
        text = f"{mins}m {secs}s"
    else:
        text = f"{secs}s"
    return f"poller +{text}" if ahead else f"mail +{text}"


def format_age(seconds: float | None) -> str:
    """'2m ago' / '1h 5m ago' style, for "when did this site last say
    anything at all" -- distinct from format_delta's poller-vs-mail race."""
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
    format_age's '... ago' and format_delta's poller-vs-mail sign."""
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


def _read_mail_events_cache() -> list[dict]:
    try:
        return json.loads(MAIL_EVENTS_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _write_mail_events_cache(events: list[dict]) -> None:
    MAIL_EVENTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    MAIL_EVENTS_CACHE.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")


def load_mail_events(days: int = 30, max_age_minutes: int = 15,
                     force: bool = False) -> list[MailEvent]:
    raw = _read_mail_events_cache()
    stale = True
    if MAIL_EVENTS_CACHE.exists():
        age = datetime.now() - datetime.fromtimestamp(MAIL_EVENTS_CACHE.stat().st_mtime)
        stale = age > timedelta(minutes=max_age_minutes)
    if force or stale:
        try:
            raw = recent_mail_events(days=days)
            _write_mail_events_cache(raw)
        except Exception:
            raw = raw or []

    # Stekkies source URLs are only known after the orchestrator extracts the
    # listing. Fill them from mail_summary records with the same Gmail msg_id or
    # Stekkies redirect URL.
    subs = load_submissions()
    by_msg = {s.msg_id: s.source_url for s in subs if s.msg_id and s.source_url}
    by_stekkies = {
        s.stekkies_url: s.source_url
        for s in subs
        if s.stekkies_url and s.source_url
    }
    events: list[MailEvent] = []
    for r in raw:
        source_url = r.get("source_url") or ""
        if r.get("provider") == "stekkies":
            source_url = source_url or by_msg.get(r.get("msg_id", ""), "")
            source_url = source_url or by_stekkies.get(r.get("stekkies_url", ""), "")
        events.append(MailEvent(
            provider=r.get("provider", ""),
            msg_id=r.get("msg_id", ""),
            received_ts=r.get("received_ts", ""),
            subject=r.get("subject", ""),
            source_url=source_url,
            stekkies_url=r.get("stekkies_url", ""),
        ))
    return events


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


def mission_kpis(subs: list[Submission], race: dict, spend: dict) -> dict:
    """The "is the mission on track" tiles for the overview: submissions,
    success rate, detection→submitted latency, race wins, spend."""
    now = datetime.now()
    week_cutoff = now - timedelta(days=7)
    attempts = [s for s in subs if s.status in ATTEMPT_STATUSES]
    submitted = [s for s in subs if s.status in ("submitted", "applied")]
    submitted_week = sum(1 for s in submitted if (s.when or datetime.min) >= week_cutoff)

    # detection → submitted latency: from when we first saw the listing
    # (poller detected_ts / mail received_ts) to the submitted record time.
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
        "poller_wins_stekkies": race["stekkies"]["poller_wins"],
        "poller_wins_huurwoningen": race["huurwoningen"]["poller_wins"],
        "spend_week_usd": spend.get("total_usd"),
        "cost_per_submission": spend.get("cost_per_submission"),
    }


def race_report(subs: list[Submission], mail_events: list[MailEvent]) -> dict:
    by_key: dict[str, list[Submission]] = defaultdict(list)
    for s in subs:
        if s.key:
            by_key[s.key].append(s)

    mail_by_key: dict[str, dict[str, list[MailEvent]]] = defaultdict(lambda: defaultdict(list))
    for e in mail_events:
        if e.key:
            mail_by_key[e.key][e.provider].append(e)

    rows: list[RaceInfo] = []
    for key, records in by_key.items():
        pollers = [s for s in records if s.origin == "poller"]
        if not pollers:
            continue
        pollers.sort(key=lambda s: s.event_time or datetime.max)
        p = pollers[0]
        p_time = p.event_time
        if p_time is None:
            continue

        stekkies_times = [e.when for e in mail_by_key[key].get("stekkies", []) if e.when]
        # Also use Stekkies-triggered submissions; they carry msg_received_ts
        # when available and are the only way to map Stekkies redirects to source URLs.
        stekkies_times += [
            s.event_time for s in records
            if s.origin == "stekkies_mail" and s.event_time
        ]
        huur_times = [e.when for e in mail_by_key[key].get("huurwoningen", []) if e.when]
        # Also use Huurwoningen-mail-triggered submissions directly, in case the
        # mail-index cache missed the event (stale refresh, API hiccup, etc.).
        huur_times += [
            s.event_time for s in records
            if s.origin == "huurwoningen_mail" and s.event_time
        ]

        st = min(stekkies_times) if stekkies_times else None
        hw = min(huur_times) if huur_times else None
        rows.append(RaceInfo(
            id=p.id,
            source_url=p.source_url,
            source=p.source,
            detected_by=p.detected_by_label,
            address=p.address,
            status=p.status,
            permalink=p.permalink,
            poller_ts=p.event_time.isoformat(timespec="seconds") if p.event_time else "",
            stekkies_ts=st.isoformat(timespec="seconds") if st else "",
            huurwoningen_ts=hw.isoformat(timespec="seconds") if hw else "",
            stekkies_lead_s=(st - p_time).total_seconds() if st else None,
            huurwoningen_lead_s=(hw - p_time).total_seconds() if hw else None,
        ))
    rows.sort(key=lambda r: r.poller_ts, reverse=True)

    def _summary(provider: str) -> dict:
        attr = "stekkies_lead_s" if provider == "stekkies" else "huurwoningen_lead_s"
        vals = [getattr(r, attr) for r in rows if getattr(r, attr) is not None]
        wins = [v for v in vals if v > 0]
        losses = [v for v in vals if v < 0]
        return {
            "matched": len(vals),
            "poller_wins": len(wins),
            "mail_wins": len(losses),
            "no_mail": len(rows) - len(vals),
            "avg_lead_s": round(sum(wins) / len(wins), 1) if wins else None,
        }

    return {
        "rows": rows,
        "stekkies": _summary("stekkies"),
        "huurwoningen": _summary("huurwoningen"),
    }


def cached_race_report() -> dict:
    """race_report over the memoized submissions + mail events, itself memoized
    so the overview/detail/submissions routes and their internal reuse share
    one computation per few seconds."""
    return cache.memo(
        "race_report", _CACHE_TTL,
        lambda: race_report(load_submissions(), load_mail_events()),
    )


# --------------------------------------------------------------------------- #
# Transcripts (redacted)
# --------------------------------------------------------------------------- #
@lru_cache(maxsize=1)
def _secret_values() -> tuple[str, ...]:
    vals: set[str] = set()
    try:
        creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        for c in creds.values():
            for k in ("password", "username"):
                v = (c.get(k) or "").strip()
                if len(v) >= 4:
                    vals.add(v)
    except Exception:
        pass
    # longest first so substrings don't pre-empt longer secrets
    return tuple(sorted(vals, key=len, reverse=True))


def redact(text: str) -> str:
    if not text:
        return text
    for v in _secret_values():
        text = text.replace(v, "***")
    text = re.sub(r"(?im)^(\s*password:).*$", r"\1 ***", text)
    text = re.sub(r"(?im)^(\s*username(?:/email)?:).*$", r"\1 ***", text)
    # belt-and-braces: redact any 'password' value the agent may have logged
    text = re.sub(r"(?i)(password['\"]?\s*[:=]\s*)\S+", r"\1***", text)
    return text


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


# --------------------------------------------------------------------------- #
# Poller site health — "is the poller actually working, and is any site
# blocking/challenging us?" This was previously invisible: the dashboard only
# showed the `poller` systemd unit as active/inactive, which says nothing
# about whether individual sites are being blocked or have silently stopped
# yielding listings (a Blocked exception backs off + retries forever without
# ever crashing the service).
# --------------------------------------------------------------------------- #
POLL_LOG = LOG_DIR / "poller.jsonl"
POLL_LOG_TAIL_LINES = 30000  # bounds memory even once the log grows for weeks


@dataclass
class SiteHealth:
    name: str
    tier: int
    enabled: bool
    needs_login: bool
    cadence_s: int
    last_ts: str = ""
    last_event: str = ""
    last_age_s: float | None = None
    polled_recent: int = 0
    blocked_recent: int = 0
    error_recent: int = 0
    new_recent: int = 0
    last_block_reason: str = ""
    last_block_ts: str = ""
    block_streak: int = 0
    status: str = "unknown"  # ok | blocked | stale | erroring | disabled | never_polled

    @property
    def status_label(self) -> str:
        return {
            "ok": "OK", "blocked": "BLOCKED", "stale": "STALE",
            "erroring": "ERRORING", "disabled": "disabled",
            "never_polled": "never polled",
        }.get(self.status, self.status)


def _poll_log_events_by_site() -> dict[str, list[dict]]:
    from collections import deque
    by_site: dict[str, list[dict]] = defaultdict(list)
    if not POLL_LOG.exists():
        return by_site
    with POLL_LOG.open(encoding="utf-8") as f:
        for line in deque(f, maxlen=POLL_LOG_TAIL_LINES):
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            site = rec.get("site")
            if site:
                by_site[site].append(rec)
    return by_site


def poller_site_health(window_hours: float = 6) -> list[SiteHealth]:
    """Per-site poller status derived from poller.jsonl, cross-referenced
    against the registry (so a site that's stopped logging entirely --
    e.g. an exception outside the try/except in _watch_site, or the whole
    poller process being down -- shows up as "stale"/"never polled" rather
    than silently vanishing)."""
    from ..poller.registry import REGISTRY

    by_site = _poll_log_events_by_site()
    now = datetime.now()
    cutoff = now - timedelta(hours=window_hours)
    results: list[SiteHealth] = []
    for cfg in REGISTRY:
        events = by_site.get(cfg.name, [])
        last = events[-1] if events else None
        recent = [e for e in events if (parse_ts(e.get("ts")) or now) >= cutoff]
        polled_recent = sum(1 for e in recent if e.get("event") == "polled")
        blocked_recent = sum(1 for e in recent if e.get("event") == "blocked")
        error_recent = sum(1 for e in recent if e.get("event") == "poll_error")
        new_recent = sum(e.get("new") or 0 for e in recent if e.get("event") == "polled")
        last_block = next((e for e in reversed(events) if e.get("event") == "blocked"), None)

        last_ts = last.get("ts", "") if last else ""
        last_dt = parse_ts(last_ts)
        last_age = (now - last_dt).total_seconds() if last_dt else None

        if not cfg.enabled:
            status = "disabled"
        elif last is None:
            status = "never_polled"
        elif last.get("event") == "blocked":
            status = "blocked"
        elif last_age is not None and last_age > max(cfg.cadence_s * 4, 900):
            status = "stale"
        elif polled_recent and error_recent >= max(polled_recent, 3):
            status = "erroring"
        else:
            status = "ok"

        results.append(SiteHealth(
            name=cfg.name, tier=cfg.tier, enabled=cfg.enabled,
            needs_login=cfg.needs_login, cadence_s=cfg.cadence_s,
            last_ts=last_ts, last_event=(last.get("event", "") if last else ""),
            last_age_s=last_age,
            polled_recent=polled_recent, blocked_recent=blocked_recent,
            error_recent=error_recent, new_recent=new_recent,
            last_block_reason=(last_block or {}).get("reason", ""),
            last_block_ts=(last_block or {}).get("ts", ""),
            block_streak=(last.get("streak") or 0) if status == "blocked" else 0,
            status=status,
        ))

    # Worst first: blocked > stale > erroring > never_polled > ok > disabled.
    order = {"blocked": 0, "stale": 1, "erroring": 2, "never_polled": 3, "ok": 4, "disabled": 5}
    results.sort(key=lambda s: (order.get(s.status, 9), s.name))
    return results


def poller_site_summary(sites: list[SiteHealth]) -> dict:
    active = [s for s in sites if s.enabled]
    return {
        "total": len(active),
        "ok": sum(1 for s in active if s.status == "ok"),
        "blocked": sum(1 for s in active if s.status == "blocked"),
        "stale": sum(1 for s in active if s.status == "stale"),
        "erroring": sum(1 for s in active if s.status == "erroring"),
        "never_polled": sum(1 for s in active if s.status == "never_polled"),
    }
