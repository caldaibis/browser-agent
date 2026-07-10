"""Email status notifications via the Gmail API (reuses the watcher's creds).

Sends a per-listing status update to NOTIFY_TO after successful submissions.
Requires the gmail.send scope (see gmail_watch.SCOPES) — re-authorize once if
your cached token predates it.

Disable with NOTIFY_ENABLED=0.
"""
import json
import time
from email.message import EmailMessage
from base64 import urlsafe_b64encode

from .config import PROJECT_ROOT
from .gmail_watch import get_service
from .settings import settings
from .eventlog import get_logger

_LOG = get_logger("notify")

_PLACEHOLDER_TO = "you@example.com"
NOTIFY_TO = settings().notify_to
# Enabled only when a real recipient is configured. Without this guard, any
# process that runs without NOTIFY_TO in its env falls back to the placeholder
# and actually emails it -- e.g. the Claude Agent SDK spawns the `claude` CLI
# with a stripped env (only ANTHROPIC_*), so a command its Bash tool runs would
# send to you@example.com and bounce (observed 08-07-2026). Treat the
# placeholder as "notifications not configured": push still fires, email does not.
NOTIFY_ENABLED = (
    settings().notify_enabled_flag
    and bool(NOTIFY_TO)
    and NOTIFY_TO != _PLACEHOLDER_TO
)
STATUS_EMAIL_OUTCOMES = {"submitted"}

# Rate-limit bookkeeping for send_alert_dedup (key -> last-sent epoch).
ALERT_DEDUP_FILE = PROJECT_ROOT / "state" / "alert_dedup.json"

# outcome -> (emoji, human label)
_OUTCOME = {
    "submitted":       ("✅", "Submitted"),
    "already_applied": ("↩️", "Already applied"),
    "not_available":   ("⌛", "Not available"),
    "not_eligible":    ("🚫", "Not eligible"),
    "login_required":  ("🔑", "Login needed"),
    "blocked":         ("⚠️", "Blocked"),
    "timeout":         ("❌", "Timed out"),
    "incomplete":      ("❌", "Incomplete"),
    "error":           ("❌", "Error"),
    "no_credit":       ("💸", "Out of API credit"),
    "payment_required": ("💳", "Payment required (not paid)"),
}


def _subject(rec: dict) -> str:
    status = rec.get("status", "unknown")
    emoji, label = _OUTCOME.get(status, ("•", status))
    addr = rec.get("address") or "unknown address"
    source = rec.get("source") or "unknown source"
    return f"{emoji} {label}: {addr} ({source})"


def _body(rec: dict) -> str:
    detected_by = rec.get("detected_by") if rec.get("trigger") == "poller" else ""
    lines = [
        f"Status:   {rec.get('status')}",
        f"Address:  {rec.get('address') or '-'}",
        f"Source:   {rec.get('source') or '-'}",
        *([f"Detected by poller: {detected_by}"] if detected_by else []),
        f"Listing:  {rec.get('source_url') or rec.get('stekkies_url') or '-'}",
        f"When:     {rec.get('ts')}",
        f"Duration: {rec.get('seconds', '-')}s",
        "",
        (rec.get("message") or "").strip(),
    ]
    return "\n".join(lines)


def send_alert(subject: str, body: str) -> None:
    """Best-effort plain alert (low credit, login expired, service down, …).

    Web push goes out FIRST and independently of the email: the Gmail token
    is itself one of the things that fails (verified 04..07-07-2026: the
    token was revoked, the orchestrator crash-looped for 3 days, and the
    healthcheck's alert email about it could never be sent because it used
    the same dead token). Push has no shared failure mode with Gmail.
    """
    try:
        from .push_notify import send_push
        send_push(title=subject, body=body[:300], tag="alert")
    except Exception as e:  # pragma: no cover - alerts are best-effort
        _LOG.info(f"alert push failed: {e}")
    if not NOTIFY_ENABLED:
        return
    try:
        msg = EmailMessage()
        msg["To"] = NOTIFY_TO
        msg["From"] = NOTIFY_TO
        msg["Subject"] = subject
        msg.set_content(body)
        raw = urlsafe_b64encode(msg.as_bytes()).decode()
        get_service().users().messages().send(userId="me", body={"raw": raw}).execute()
        _LOG.info(f"alert sent: {subject}")
    except Exception as e:  # pragma: no cover
        _LOG.info(f"alert send failed: {e}")


def send_alert_dedup(key: str, subject: str, body: str,
                     min_interval_s: float = 3600.0) -> bool:
    """send_alert, rate-limited per ``key``: at most one alert per
    ``min_interval_s`` seconds. For conditions that would otherwise fire on
    every poll/turn/listing (browser lock contention, credit exhaustion, a
    zero-yield site) — one push per hour beats forty. Never raises; returns
    whether the alert was actually sent."""
    now = time.time()
    state: dict = {}
    try:
        state = json.loads(ALERT_DEDUP_FILE.read_text(encoding="utf-8"))
    except Exception:
        state = {}
    try:
        if now - float(state.get(key, 0)) < min_interval_s:
            return False
    except (TypeError, ValueError):
        pass
    state[key] = now
    try:
        ALERT_DEDUP_FILE.parent.mkdir(parents=True, exist_ok=True)
        ALERT_DEDUP_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception as e:  # pragma: no cover - bookkeeping is best-effort
        _LOG.info(f"alert dedup state write failed: {e}")
    send_alert(subject, body)
    return True


def send_status_email(rec: dict) -> None:
    """Best-effort: email successful submissions only.

    Never raises: logging/apply flow must not break on a mail failure.
    """
    # Web push piggybacks on this single integration point (both the
    # orchestrator and the poller route every outcome through here). It has
    # its own enable flag + outcome filter and never raises.
    from .push_notify import push_status
    push_status(rec)
    if not NOTIFY_ENABLED:
        return
    if rec.get("status") not in STATUS_EMAIL_OUTCOMES:
        return
    try:
        msg = EmailMessage()
        msg["To"] = NOTIFY_TO
        msg["From"] = NOTIFY_TO
        msg["Subject"] = _subject(rec)
        msg.set_content(_body(rec))
        raw = urlsafe_b64encode(msg.as_bytes()).decode()
        get_service().users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as e:  # pragma: no cover - notifications are best-effort
        _LOG.info(f"failed to send status email: {e}")
