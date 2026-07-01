"""Email status notifications via the Gmail API (reuses the watcher's creds).

Sends a per-listing status update to NOTIFY_TO after successful submissions.
Requires the gmail.send scope (see gmail_watch.SCOPES) — re-authorize once if
your cached token predates it.

Disable with NOTIFY_ENABLED=0.
"""
import os
from email.message import EmailMessage
from base64 import urlsafe_b64encode

from .gmail_watch import get_service

NOTIFY_TO = os.environ.get("NOTIFY_TO", "you@example.com")
NOTIFY_ENABLED = os.environ.get("NOTIFY_ENABLED", "1") != "0"
STATUS_EMAIL_OUTCOMES = {"submitted"}

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
}


def _subject(rec: dict) -> str:
    status = rec.get("status", "unknown")
    emoji, label = _OUTCOME.get(status, ("•", status))
    addr = rec.get("address") or "unknown address"
    source = rec.get("source") or "unknown source"
    return f"{emoji} {label}: {addr} ({source})"


def _body(rec: dict) -> str:
    lines = [
        f"Status:   {rec.get('status')}",
        f"Address:  {rec.get('address') or '-'}",
        f"Source:   {rec.get('source') or '-'}",
        f"Listing:  {rec.get('source_url') or rec.get('stekkies_url') or '-'}",
        f"When:     {rec.get('ts')}",
        f"Duration: {rec.get('seconds', '-')}s",
        "",
        (rec.get("message") or "").strip(),
    ]
    return "\n".join(lines)


def send_alert(subject: str, body: str) -> None:
    """Best-effort plain alert email (low credit, login expired, …)."""
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
        print(f"[notify] alert sent: {subject}")
    except Exception as e:  # pragma: no cover
        print(f"[notify] alert send failed: {e}")


def send_status_email(rec: dict) -> None:
    """Best-effort: email successful submissions only.

    Never raises: logging/apply flow must not break on a mail failure.
    """
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
        print(f"[notify] failed to send status email: {e}")
