"""Web-push notifications (Chrome desktop + Android) for submission events.

Standard Web Push API with VAPID — no third-party account, no app to build:
the user opens the dashboard once per device, clicks the 🔔 button, and the
browser (Chrome on desktop, Chrome on Android — which delivers even with the
browser closed) receives a native notification whenever a listing outcome in
WEB_PUSH_OUTCOMES (default: submitted) is recorded.

Pieces:
  - VAPID keypair: auto-generated on first use, persisted in
    ``state/vapid.json`` (gitignored, like all state/).
  - Subscriptions: one JSON per line in ``state/push_subscriptions.jsonl``,
    deduped by endpoint; expired endpoints (HTTP 404/410 from the push
    service) are pruned automatically on send.
  - Sending happens in the orchestrator process, via notify.send_status_email's
    hook — the dashboard only serves the public key + subscribe/unsubscribe
    endpoints and the service worker.

Everything is best-effort and fail-open: notifications must never break the
apply flow. Disable entirely with WEB_PUSH_ENABLED=0.
"""
from __future__ import annotations

import base64
import json

from .config import PROJECT_ROOT
from .settings import settings
from .eventlog import get_logger

_LOG = get_logger("push")
from .eventlog import utc_now_iso

VAPID_FILE = PROJECT_ROOT / "state" / "vapid.json"
SUBSCRIPTIONS_FILE = PROJECT_ROOT / "state" / "push_subscriptions.jsonl"

WEB_PUSH_ENABLED = settings().web_push_enabled
WEB_PUSH_OUTCOMES = set(settings().web_push_outcomes)
# VAPID requires a contact claim; reuse the notification mailbox.
_CONTACT = settings().notify_to


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _ensure_keys() -> dict:
    """Load the VAPID keypair, generating + persisting it on first use.

    Stored as base64url raw values: ``private`` is the 32-byte P-256 private
    value (exactly what pywebpush's ``vapid_private_key=`` accepts as a
    string), ``public`` is the 65-byte uncompressed point (exactly what the
    browser's ``applicationServerKey`` wants)."""
    try:
        keys = json.loads(VAPID_FILE.read_text(encoding="utf-8"))
        if keys.get("private") and keys.get("public"):
            return keys
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.generate_private_key(ec.SECP256R1())
    keys = {
        "private": _b64url(
            key.private_numbers().private_value.to_bytes(32, "big")),
        "public": _b64url(key.public_key().public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)),
    }
    VAPID_FILE.parent.mkdir(parents=True, exist_ok=True)
    VAPID_FILE.write_text(json.dumps(keys), encoding="utf-8")
    return keys


def public_key() -> str:
    """The applicationServerKey for the browser's pushManager.subscribe."""
    return _ensure_keys()["public"]


def list_subscriptions() -> list[dict]:
    subs: dict[str, dict] = {}
    if not SUBSCRIPTIONS_FILE.exists():
        return []
    for line in SUBSCRIPTIONS_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        endpoint = (rec.get("subscription") or {}).get("endpoint")
        if not endpoint:
            continue
        if rec.get("removed"):
            subs.pop(endpoint, None)
        else:
            subs[endpoint] = rec["subscription"]
    return list(subs.values())


def add_subscription(subscription: dict, user_agent: str = "") -> None:
    if not (subscription or {}).get("endpoint"):
        raise ValueError("subscription has no endpoint")
    if subscription in list_subscriptions():
        return
    SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": utc_now_iso(),
           "subscription": subscription, "user_agent": user_agent[:200]}
    with SUBSCRIPTIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def remove_subscription(endpoint: str) -> None:
    if not endpoint:
        return
    SUBSCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": utc_now_iso(),
           "subscription": {"endpoint": endpoint}, "removed": True}
    with SUBSCRIPTIONS_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _send_one(subscription: dict, payload: dict, keys: dict) -> None:
    """One webpush send; prunes the subscription when the push service says
    it is gone (device unsubscribed / browser reinstalled)."""
    from pywebpush import webpush, WebPushException
    try:
        webpush(
            subscription_info=subscription,
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=keys["private"],
            vapid_claims={"sub": f"mailto:{_CONTACT}"},
            ttl=3600,
        )
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            remove_subscription(subscription.get("endpoint", ""))
            _LOG.info(f"pruned expired subscription (HTTP {status})")
        else:
            _LOG.info(f"send failed: {e}")


def send_push(title: str, body: str, url: str = "/", tag: str = "stekkies") -> int:
    """Send a notification to every subscribed device. Returns how many
    subscriptions were attempted. Never raises."""
    if not WEB_PUSH_ENABLED:
        return 0
    try:
        subs = list_subscriptions()
        if not subs:
            return 0
        keys = _ensure_keys()
        payload = {"title": title, "body": body, "url": url, "tag": tag}
        for sub in subs:
            _send_one(sub, payload, keys)
        return len(subs)
    except Exception as e:  # noqa: BLE001 - notifications are best-effort
        _LOG.info(f"failed: {type(e).__name__}: {e}")
        return 0


def push_status(rec: dict) -> None:
    """Push a listing-outcome record (same dict send_status_email gets) to
    all devices, when its status is in WEB_PUSH_OUTCOMES. Never raises."""
    if rec.get("status") not in WEB_PUSH_OUTCOMES:
        return
    # Late import: notify imports this module, so pull the subject formatter
    # lazily to keep the dependency one-directional at import time.
    from .notify import _subject
    body = " · ".join(
        str(part) for part in (rec.get("source"), rec.get("message"))
        if part
    )[:180]
    send_push(title=_subject(rec), body=body, url="/",
              tag=rec.get("source_url") or "stekkies")
