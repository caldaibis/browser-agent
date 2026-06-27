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
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from ..config import LOG_DIR, PROJECT_ROOT
from ..healthcheck import remaining_credit, CREDIT_THRESHOLD_USD

MAIL_SUMMARY = LOG_DIR / "mail_summary.jsonl"
ACTIVITY_LOG = LOG_DIR / "activity.log"
TRANSCRIPTS_DIR = LOG_DIR / "transcripts"
SCREENSHOTS_DIR = LOG_DIR / "screenshots"
ALERTS_FILE = PROJECT_ROOT / "state" / "alerts.json"
CREDS_FILE = PROJECT_ROOT / "state" / "sources_credentials.json"

# Outcomes that represent a real apply attempt (for success-rate maths).
ATTEMPT_STATUSES = {
    "submitted", "already_applied", "not_available", "not_eligible",
    "login_required", "blocked", "timeout", "incomplete", "error",
}
SERVICES = ["orchestrator", "browser-host", "xvfb", "healthcheck.timer"]


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

    @property
    def when(self) -> datetime | None:
        try:
            return datetime.fromisoformat(self.ts)
        except Exception:
            return None


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
        ))
    return out


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
    submitted = by_status.get("submitted", 0)
    by_source = Counter(s.source for s in attempts if s.source)
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
        "per_day": dict(sorted(per_day.items())),
        "avg_seconds": round(sum(durations) / len(durations), 1) if durations else 0.0,
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
            out[svc] = (r.stdout or r.stderr or "unknown").strip()
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
    credit = remaining_credit()
    return {
        "services": service_status(),
        "credit": credit,
        "credit_low": (credit is not None and credit < CREDIT_THRESHOLD_USD),
        "credit_threshold": CREDIT_THRESHOLD_USD,
        **login_health(),
    }


def recent_activity(n: int = 25) -> list[str]:
    if not ACTIVITY_LOG.exists():
        return []
    lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
    return lines[-n:][::-1]
