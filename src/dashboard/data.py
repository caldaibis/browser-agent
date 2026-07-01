"""Read-only data layer for the dashboard.

Parses the agent's existing log/state files (no DB) and exposes submissions,
stats, redacted transcripts, and live health. SECURITY: transcripts may contain
credentials (the apply prompt embeds site passwords); everything served goes
through redact(), and *.prompt.txt is never read here.
"""
from __future__ import annotations

import json
import re
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

from ..config import LOG_DIR, PROJECT_ROOT
from ..gmail_watch import recent_mail_events
from ..healthcheck import remaining_credit, CREDIT_THRESHOLD
from ..poller.dedup import canonical_url

MAIL_SUMMARY = LOG_DIR / "mail_summary.jsonl"
ACTIVITY_LOG = LOG_DIR / "activity.log"
TRANSCRIPTS_DIR = LOG_DIR / "transcripts"
SCREENSHOTS_DIR = LOG_DIR / "screenshots"
ALERTS_FILE = PROJECT_ROOT / "state" / "alerts.json"
CREDS_FILE = PROJECT_ROOT / "state" / "sources_credentials.json"
MAIL_EVENTS_CACHE = PROJECT_ROOT / "state" / "dashboard_mail_events.json"

# Outcomes that represent a real apply attempt (for success-rate maths).
ATTEMPT_STATUSES = {
    "submitted", "applied", "already_applied", "not_available", "not_eligible",
    "login_required", "blocked", "timeout", "incomplete", "error",
}
SERVICES = ["orchestrator", "poller", "browser-host", "xvfb", "dashboard", "healthcheck.timer"]


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
        return canonical_url(self.source_url) if self.source_url else ""


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
    source_url: str
    source: str
    detected_by: str
    address: str
    status: str
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


def load_submissions() -> list[Submission]:
    out: list[Submission] = []
    if not MAIL_SUMMARY.exists():
        return out
    for i, line in enumerate(MAIL_SUMMARY.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append(Submission(
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
        ))
    return out


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
    by_msg = {s.msg_id: s.source_url for s in load_submissions() if s.msg_id and s.source_url}
    by_stekkies = {
        s.stekkies_url: s.source_url
        for s in load_submissions()
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


def get_submission(sub_id: int) -> Submission | None:
    for s in load_submissions():
        if s.id == sub_id:
            return s
    return None


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

        st = min(stekkies_times) if stekkies_times else None
        hw = min(huur_times) if huur_times else None
        rows.append(RaceInfo(
            source_url=p.source_url,
            source=p.source,
            detected_by=p.detected_by_label,
            address=p.address,
            status=p.status,
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
# Health
# --------------------------------------------------------------------------- #
def service_status() -> dict[str, str]:
    out: dict[str, str] = {}
    for svc in SERVICES:
        try:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True, timeout=5)
            text = (r.stdout or r.stderr or "unknown").strip()
            first = text.splitlines()[0] if text else "unknown"
            if "System has not been booted with systemd" in text:
                first = "unavailable"
            out[svc] = first[:80]
        except Exception:
            out[svc] = "unknown"
    return out


def login_health() -> dict:
    alerts, last_check = {}, None
    try:
        alerts = json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
        last_check = datetime.fromtimestamp(ALERTS_FILE.stat().st_mtime).isoformat(timespec="seconds")
    except Exception:
        pass
    return {
        "stekkies_logged_in": not alerts.get("stekkies_logout_sent", False),
        "low_credit": alerts.get("low_credit_sent", False),
        "last_health_check": last_check,
    }


def health() -> dict:
    credit_info = remaining_credit()
    credit = credit_info[0] if credit_info is not None else None
    credit_currency = credit_info[1] if credit_info is not None else None
    return {
        "services": service_status(),
        "credit": credit,
        "credit_currency": credit_currency,
        "credit_low": (credit is not None and credit < CREDIT_THRESHOLD),
        "credit_threshold": CREDIT_THRESHOLD,
        **login_health(),
    }


def recent_activity(n: int = 25) -> list[str]:
    if not ACTIVITY_LOG.exists():
        return []
    lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
    return lines[-n:][::-1]
