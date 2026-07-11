"""System health + the overview "action needed" strip.

Split out of data.py (which is about submissions/costs/transcripts). This is
the "does the operator need to do something right now" layer: live systemd
service state, DeepSeek credit, login/session freshness, and the aggregated
attention items (pending patches, paid gates, self-improvement failing,
stuck browser lock).
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from datetime import datetime

from ..config import LOG_DIR, PROJECT_ROOT
from ..healthcheck import CREDIT_THRESHOLD, remaining_credit
from . import cache

ALERTS_FILE = PROJECT_ROOT / "state" / "alerts.json"
ACTIVITY_LOG = LOG_DIR / "activity.log"
PENDING_PATCH_DIR = PROJECT_ROOT / "state" / "pending_patches"
BROWSER_LOCK = PROJECT_ROOT / "state" / "browser.lock"
SI_RUN_LOG = LOG_DIR / "self_improvement.jsonl"

SERVICES = ["orchestrator", "browser-host", "xvfb", "dashboard", "healthcheck.timer"]

# Same rule as healthcheck.check_self_improvement: N consecutive failed runs.
SI_HEALTH_WINDOW = 5
_SI_FAILURE_ACTIONS = {"error", "fix_failed", "timeout", "incomplete"}

# A held browser lock older than this is almost certainly wedged (a normal
# apply finishes well under 15 min; the watchdog SIGKILLs at ~timeout+grace).
BROWSER_LOCK_STUCK_SECONDS = 35 * 60
CREDIT_CACHE_SECONDS = 20.0

_credit_lock = threading.Lock()
_credit_value: tuple[float, str] | None = None
_credit_checked_monotonic = 0.0


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


def _cached_credit(fresh_only: bool) -> tuple[float, str] | None:
    with _credit_lock:
        if not _credit_checked_monotonic:
            return None
        if not fresh_only or time.monotonic() - _credit_checked_monotonic <= CREDIT_CACHE_SECONDS:
            return _credit_value
    return None


def refresh_credit() -> tuple[float, str] | None:
    """Refresh DeepSeek credit off the request path."""
    global _credit_value, _credit_checked_monotonic
    credit = remaining_credit()
    with _credit_lock:
        _credit_value = credit
        _credit_checked_monotonic = time.monotonic()
    return credit


def health(refresh_credit_if_stale: bool = True) -> dict:
    key = "health" if refresh_credit_if_stale else "health:cached_credit"

    def _build() -> dict:
        credit_info = _cached_credit(fresh_only=refresh_credit_if_stale)
        if credit_info is None and refresh_credit_if_stale:
            credit_info = refresh_credit()
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
    # remaining_credit() hits the DeepSeek API; cache a little longer than the
    # 30s health poll so repeated polls don't each make a network call.
    return cache.memo(key, 20.0, _build)


def recent_activity(n: int = 25) -> list[str]:
    if not ACTIVITY_LOG.exists():
        return []
    lines = ACTIVITY_LOG.read_text(encoding="utf-8").splitlines()
    return lines[-n:][::-1]


def _pending_patches() -> list[str]:
    try:
        return sorted(p.name for p in PENDING_PATCH_DIR.glob("*.patch"))
    except OSError:
        return []


def _si_failing_streak() -> bool:
    runs: list[bool] = []  # True == failed
    for rec in cache.jsonl_records(SI_RUN_LOG):
        event = rec.get("event")
        if event == "error":
            runs.append(True)
        elif event == "done":
            runs.append(str(rec.get("action") or "") in _SI_FAILURE_ACTIONS)
    recent = runs[-SI_HEALTH_WINDOW:]
    return len(recent) >= SI_HEALTH_WINDOW and all(recent)


def _browser_lock_stuck() -> tuple[bool, str]:
    try:
        st = BROWSER_LOCK.stat()
    except OSError:
        return False, ""
    age = (datetime.now() - datetime.fromtimestamp(st.st_mtime)).total_seconds()
    if age < BROWSER_LOCK_STUCK_SECONDS:
        return False, ""
    try:
        holder = BROWSER_LOCK.read_text(encoding="utf-8").strip()
    except OSError:
        holder = ""
    return True, holder


def attention_items() -> list[dict]:
    """Ranked list of things that need the operator's attention right now.
    Each item: {severity: bad|warn, title, detail, action(optional command),
    href(optional dashboard link)}. Empty list == all clear."""
    def _build() -> list[dict]:
        from ..known_gates import load_gates

        items: list[dict] = []
        h = health(refresh_credit_if_stale=False)

        for svc, st in h["services"].items():
            if st in ("failed", "inactive"):
                items.append({
                    "severity": "bad", "title": f"Service {svc} is {st}",
                    "detail": "This part of the pipeline is processing nothing.",
                    "action": f"ssh <vps> 'journalctl -u {svc} -n 50'",
                })

        if h["credit"] is not None and h["credit_low"]:
            items.append({
                "severity": "bad", "title": "DeepSeek credit low",
                "detail": f"{h['credit_currency']} {h['credit']:.2f} "
                          f"(threshold {h['credit_threshold']:.2f}) — applies stop at 0.",
                "action": "top up at https://platform.deepseek.com/top_up",
            })

        if not h["stekkies_logged_in"]:
            items.append({
                "severity": "bad", "title": "Stekkies session logged out",
                "detail": "Applies fail with login_required until you re-login (VNC).",
            })

        patches = _pending_patches()
        if patches:
            items.append({
                "severity": "warn",
                "title": f"{len(patches)} verified fix(es) not deployed",
                "detail": "A self-improvement fix passed verification but could not "
                          "be pushed. Apply it by hand so it isn't lost.",
                "action": f"git am state/pending_patches/{patches[-1]}",
                "href": "/self-improvement",
            })

        try:
            paid = [g for g in load_gates() if g.get("kind") == "paid_registration"]
        except Exception:
            paid = []
        if paid:
            names = ", ".join(sorted({str(g.get("domain")) for g in paid}))
            items.append({
                "severity": "warn",
                "title": f"{len(paid)} site(s) auto-skipped as paid",
                "detail": f"Applications are being skipped on: {names}. "
                          "Remove the gate if this is wrong.",
                "href": "/self-improvement",
            })

        if _si_failing_streak():
            items.append({
                "severity": "bad", "title": "Self-improvement keeps failing",
                "detail": f"The last {SI_HEALTH_WINDOW} self-improvement runs all "
                          "failed — the layer that repairs failures is broken/blocked.",
                "href": "/self-improvement",
            })

        stuck, holder = _browser_lock_stuck()
        if stuck:
            items.append({
                "severity": "bad", "title": "Browser lock looks stuck",
                "detail": f"Held > 35 min ({holder or 'unknown holder'}). The shared "
                          "browser may be wedged; no applies can run.",
            })

        order = {"bad": 0, "warn": 1}
        items.sort(key=lambda it: order.get(it["severity"], 9))
        return items
    return cache.memo("attention", 15.0, _build)


def warm_dashboard_caches() -> None:
    """Populate expensive health/attention values off the request path."""
    try:
        refresh_credit()
    except Exception:
        pass
    try:
        health(refresh_credit_if_stale=False)
    except Exception:
        pass
    try:
        attention_items()
    except Exception:
        pass
