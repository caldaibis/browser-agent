"""Proactive login-session repair for source sites the apply agent depends on.

The healthcheck's site-login probe (`healthcheck.check_site_logins`) can only
detect a dead session and email a manual-VNC alert. Every listing on that site
then fails with `login_required` until a human re-logs in by hand. This module
is a deterministic (no LLM, no MCP), narrowly-scoped Playwright adapter that
attempts the repair itself: prefer Google SSO (the shared browser profile is
already Google-logged-in, see `browser_host.py`), fall back to a stored
password, never touch a password-reset flow, and verify against the site's own
authenticated page before declaring success.

State persists per domain in `state/session_keeper.json` (same atomic
tempfile+os.replace idiom as `known_gates.py`) — this doubles as the "durable
queue": both call sites below are pull-based, consulting this file for
cooldown/backoff instead of a separate worker process:
  - `healthcheck.check_site_logins` calls `ensure_session(domain, ctx=ctx)`
    using the browser context it already holds the lock for (every 30 min).
  - `apply.apply()` calls `ensure_session(domain)` as a preflight, before it
    acquires its own browser lock for the actual application run, so a
    time-critical mail-triggered apply never wastes turns discovering a dead
    session mid-form.

Alerting happens once per "blocker episode" (same idiom as healthcheck's own
`logout_sent:{name}` dedup) and only when autonomous repair actually failed.
Only ONE failure class is fed to self-improvement: `adapter_error` — the login
flow ran to completion but the site still looks logged out, meaning this
module's own assumptions (selectors/button text) are stale. CAPTCHA, 2FA,
account-chooser mismatches, and rejected passwords are real external gates
only a human can clear; they are alerted, never handed to the code-repair
agent.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from . import credentials
from .config import CDP_URL, PROJECT_ROOT
from .eventlog import get_logger, parse_ts, utc_now_iso
from .browser_lock import browser_lock
from .settings import settings

_LOG = get_logger("session_keeper")

STATE_PATH = PROJECT_ROOT / "state" / "session_keeper.json"

SESSION_KEEPER_ENABLED = settings().session_keeper_enabled
COOLDOWN_SECONDS = settings().session_keeper_cooldown_seconds
LOCK_TIMEOUT_SECONDS = settings().session_keeper_lock_timeout_seconds
GOOGLE_ACCOUNT = settings().google_account

# Failure kinds that mean "an external gate a human must clear" -- alerted,
# never fed to self-improvement.
_HUMAN_GATE_OUTCOMES = frozenset({
    "captcha", "twofactor", "account_chooser", "credentials_rejected",
})

_CAPTCHA_RE = re.compile(
    r"(captcha|verifieer dat je een mens bent|are you a human|i'm not a robot)",
    re.IGNORECASE,
)
_TWOFACTOR_RE = re.compile(
    r"(verificatiecode|(two|2)-step verification|two-factor|2fa|authenticator|"
    r"bevestig dat jij het bent|verify it'?s you)",
    re.IGNORECASE,
)
_REJECTED_RE = re.compile(
    r"(onjuist(e)? (wachtwoord|e-?mailadres)|incorrect (password|email)|"
    r"ongeldig(e)? (wachtwoord|inloggegevens)|invalid credentials)",
    re.IGNORECASE,
)
_GOOGLE_SSO_RE = re.compile(
    r"(ga verder|doorgaan|continue|sign in|log in)\s+(met|with)\s+google",
    re.IGNORECASE,
)
_FORGOT_PASSWORD_RE = re.compile(
    r"(wachtwoord vergeten|forgot password|reset (je|your) wachtwoord|"
    r"reset password)",
    re.IGNORECASE,
)


def _looks_like_captcha(text: str) -> bool:
    return bool(_CAPTCHA_RE.search(text or ""))


def _looks_like_2fa(text: str) -> bool:
    return bool(_TWOFACTOR_RE.search(text or ""))


def _looks_like_rejected_credentials(text: str) -> bool:
    return bool(_REJECTED_RE.search(text or ""))


def _is_google_sso_button(text: str) -> bool:
    return bool(_GOOGLE_SSO_RE.search(text or ""))


def _is_forgot_password(text: str) -> bool:
    return bool(_FORGOT_PASSWORD_RE.search(text or ""))


def _domain(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class RepairOutcome:
    domain: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class SiteAdapter:
    domain: str
    probe_url: str
    login_url: str
    cooldown_seconds: int
    login: Callable[[Any, dict | None], RepairOutcome]


def _login_huurwoningen(page: Any, credential: dict | None) -> RepairOutcome:
    """Deterministic login driver for huurwoningen.nl. Returns a RepairOutcome
    with one of the "attempted" sub-outcomes (captcha/twofactor/
    account_chooser/credentials_rejected/skipped_no_credential), or "ok" if the
    flow completed with no detected blocker (the caller re-probes to confirm)."""
    domain = "huurwoningen.nl"
    page.goto("https://www.huurwoningen.nl/account/inloggen/",
              wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1000)
    body_text = _safe_text(page)
    if _looks_like_captcha(body_text):
        return RepairOutcome(domain, "captcha", "CAPTCHA shown on the login page")

    if _try_google_sso(page):
        page.wait_for_timeout(1500)
        body_text = _safe_text(page)
        if _looks_like_2fa(body_text):
            return RepairOutcome(domain, "twofactor",
                                 "Google requested 2FA/verification during SSO")
        if "accounts.google.com" in page.url and _account_chooser_mismatch(page):
            return RepairOutcome(domain, "account_chooser",
                                 f"Google account chooser did not offer {GOOGLE_ACCOUNT}")
        # SSO click resolved without a detected blocker; let the caller re-probe.
        return RepairOutcome(domain, "ok", "attempted Google SSO")

    if not credential:
        return RepairOutcome(domain, "skipped_no_credential",
                             "no Google SSO offered and no stored credential")

    if not _fill_password_login(page, credential):
        return RepairOutcome(domain, "adapter_error",
                             "could not locate email/password fields or submit control")

    page.wait_for_timeout(1500)
    body_text = _safe_text(page)
    if _looks_like_captcha(body_text):
        return RepairOutcome(domain, "captcha", "CAPTCHA shown after password submit")
    if _looks_like_2fa(body_text):
        return RepairOutcome(domain, "twofactor", "2FA requested after password submit")
    if _looks_like_rejected_credentials(body_text):
        return RepairOutcome(domain, "credentials_rejected",
                             "stored password was rejected")
    return RepairOutcome(domain, "ok", "attempted password login")


def _safe_text(page: Any) -> str:
    try:
        return page.locator("body").inner_text(timeout=3000)
    except Exception:
        return ""


def _first_visible(locator: Any) -> Any:
    try:
        count = locator.count()
    except Exception:
        return None
    for i in range(count):
        item = locator.nth(i)
        try:
            if item.is_visible(timeout=200):
                return item
        except Exception:
            continue
    return None


def _try_google_sso(page: Any) -> bool:
    try:
        candidates = page.get_by_text(_GOOGLE_SSO_RE)
    except Exception:
        return False
    button = _first_visible(candidates)
    if button is None:
        return False
    try:
        button.click(timeout=3000)
        page.wait_for_timeout(1000)
        return True
    except Exception:
        return False


def _account_chooser_mismatch(page: Any) -> bool:
    """True when Google is showing an account chooser that does NOT offer
    GOOGLE_ACCOUNT (a real mismatch), False if the account was found and
    clicked or no chooser is showing at all."""
    try:
        tile = _first_visible(page.get_by_text(GOOGLE_ACCOUNT, exact=False))
    except Exception:
        tile = None
    if tile is not None:
        try:
            tile.click(timeout=3000)
            page.wait_for_timeout(1000)
            return False
        except Exception:
            return True
    # No matching tile: only a real mismatch if a chooser is actually showing.
    try:
        return page.get_by_text(re.compile(r"kies een account|choose an account", re.I)).count() > 0
    except Exception:
        return False


def _fill_password_login(page: Any, credential: dict) -> bool:
    username = str(credential.get("username") or "")
    password = str(credential.get("password") or "")
    if not username or not password:
        return False
    email_field = _first_visible(page.locator(
        "input[type='email'], input[name*='email' i], input[name*='username' i]"))
    if email_field is None:
        return False
    try:
        email_field.fill(username, timeout=3000)
    except Exception:
        return False
    password_field = _first_visible(page.locator("input[type='password']"))
    if password_field is None:
        return False
    try:
        password_field.fill(password, timeout=3000)
    except Exception:
        return False
    submit = _first_visible(page.locator(
        "button[type='submit'], input[type='submit']"))
    if submit is None:
        return False
    try:
        # Never a password-reset control: refuse if the only clickable text
        # near a submit-shaped element is a forgot-password link.
        label = ""
        try:
            label = submit.inner_text(timeout=500)
        except Exception:
            pass
        if _is_forgot_password(label):
            return False
        submit.click(timeout=3000)
        return True
    except Exception:
        return False


def _logged_out(page: Any) -> bool:
    url = page.url.lower()
    if any(m in url for m in ("/login", "inloggen", "aanmelden", "/signin")):
        return True
    try:
        return page.locator("input[type=password]").count() > 0
    except Exception:
        return False


ADAPTERS: dict[str, SiteAdapter] = {
    "huurwoningen.nl": SiteAdapter(
        domain="huurwoningen.nl",
        probe_url="https://www.huurwoningen.nl/mijn-huurwoningen/",
        login_url="https://www.huurwoningen.nl/account/inloggen/",
        cooldown_seconds=COOLDOWN_SECONDS,
        login=_login_huurwoningen,
    ),
}


def has_adapter(domain: str) -> bool:
    return _domain(domain) in ADAPTERS or domain in ADAPTERS


def _get_adapter(domain: str) -> SiteAdapter | None:
    return ADAPTERS.get(domain) or ADAPTERS.get(_domain(domain))


# ---------------------------------------------------------------- state ----
def _load_state() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_state(data: dict[str, dict[str, Any]]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _domain_state(domain: str) -> dict[str, Any]:
    return _load_state().get(domain, {})


def _cooldown_active(domain: str, *, now: datetime | None = None) -> tuple[bool, dict[str, Any]]:
    st = _domain_state(domain)
    next_after = parse_ts(st.get("next_attempt_after_ts"))
    now = now or datetime.now()
    if next_after is not None and next_after > now:
        return True, st
    return False, st


def _record_success(domain: str, *, repaired: bool) -> None:
    data = _load_state()
    data[domain] = {
        "status": "ok",
        "last_check_ts": utc_now_iso(),
        "last_success_ts": utc_now_iso(),
        "consecutive_failures": 0,
        "next_attempt_after_ts": None,
        "last_blocker_kind": "",
        "last_blocker": "",
        "alert_sent": False,
    }
    _write_state(data)
    _LOG.info(f"{domain}: {'repaired' if repaired else 'session ok'}")


def _record_failure(domain: str, adapter: SiteAdapter, outcome: str, detail: str) -> bool:
    """Persist a failed repair episode. Returns True if this is a NEW alert
    (transition into failure, or a changed blocker kind) vs. a repeat of an
    already-alerted episode."""
    data = _load_state()
    prev = data.get(domain, {})
    consecutive = int(prev.get("consecutive_failures") or 0) + 1
    backoff = adapter.cooldown_seconds * min(consecutive, 4)
    next_after = datetime.now().timestamp() + backoff
    prev_blocker_kind = prev.get("last_blocker_kind")
    prev_alert_sent = bool(prev.get("alert_sent"))
    should_alert = not prev_alert_sent or prev_blocker_kind != outcome
    data[domain] = {
        "status": "blocked",
        "last_check_ts": utc_now_iso(),
        "last_success_ts": prev.get("last_success_ts"),
        "consecutive_failures": consecutive,
        "next_attempt_after_ts": datetime.fromtimestamp(next_after).isoformat(),
        "last_blocker_kind": outcome,
        "last_blocker": detail,
        "alert_sent": True,
    }
    _write_state(data)
    _LOG.info(f"{domain}: repair failed ({outcome}): {detail}")
    return should_alert


# -------------------------------------------------------------- alerting ---
_BLOCKER_LABELS = {
    "captcha": "a CAPTCHA challenge",
    "twofactor": "a 2FA/verification challenge",
    "account_chooser": "a Google account-chooser mismatch",
    "credentials_rejected": "a rejected stored password",
    "adapter_error": "the login flow completing without restoring the session "
                     "(the site's login page likely changed)",
    "skipped_no_credential": "no stored credential and no Google SSO offered",
}


def _alert_repair_failed(adapter: SiteAdapter, outcome: RepairOutcome) -> None:
    try:
        from .notify import send_alert
        from .healthcheck import SERVER_HINT
    except Exception as e:  # noqa: BLE001 - alerting must never break repair
        _LOG.info(f"alert import failed: {e}")
        return
    label = _BLOCKER_LABELS.get(outcome.outcome, outcome.outcome)
    try:
        send_alert(
            f"🔑 Stekkies bot: automatic repair failed for {adapter.domain}",
            f"session_keeper tried to restore the {adapter.domain} login "
            f"session automatically and could not: {label}.\n\n"
            f"Detail: {outcome.detail}\n\n"
            "Applications on this site fail with login_required until this is "
            "resolved by hand. Re-login via VNC:\n\n"
            f"  ssh {SERVER_HINT} 'systemctl start vnc.service'\n"
            f"  ssh -L 5900:localhost:5900 {SERVER_HINT}\n"
            f"  # connect a VNC viewer to localhost:5900, resolve the "
            f"{label} on {adapter.login_url}, then:\n"
            f"  ssh {SERVER_HINT} 'systemctl stop vnc.service'\n",
        )
    except Exception as e:  # noqa: BLE001
        _LOG.info(f"alert send failed: {e}")


def _feed_self_improvement(adapter: SiteAdapter, outcome: RepairOutcome) -> None:
    try:
        from .self_improvement_agent import improve_session_keeper_adapter
        improve_session_keeper_adapter(
            domain=adapter.domain, detail=outcome.detail,
            probe_url=adapter.probe_url, login_url=adapter.login_url,
        )
    except Exception as e:  # noqa: BLE001 - must never break repair
        _LOG.info(f"self-improvement handoff failed: {e}")


# ------------------------------------------------------------- core logic --
def _run_probe_and_repair(domain: str, adapter: SiteAdapter, ctx: Any) -> RepairOutcome:
    page = ctx.new_page()
    try:
        page.goto(adapter.probe_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        if not _logged_out(page):
            _record_success(domain, repaired=False)
            return RepairOutcome(domain, "ok")

        credential = credentials.lookup(domain)
        attempt = adapter.login(page, credential)
        if attempt.outcome in _HUMAN_GATE_OUTCOMES or attempt.outcome == "skipped_no_credential":
            should_alert = _record_failure(domain, adapter, attempt.outcome, attempt.detail)
            if should_alert:
                _alert_repair_failed(adapter, attempt)
            return attempt

        # attempt.outcome in {"ok", "adapter_error"}: re-probe to confirm.
        page.goto(adapter.probe_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1000)
        if not _logged_out(page):
            _record_success(domain, repaired=True)
            return RepairOutcome(domain, "repaired")

        final = RepairOutcome(
            domain, "adapter_error",
            attempt.detail or "login flow completed but session was not restored")
        should_alert = _record_failure(domain, adapter, final.outcome, final.detail)
        if should_alert:
            _alert_repair_failed(adapter, final)
        _feed_self_improvement(adapter, final)
        return final
    finally:
        try:
            page.close()
        except Exception:
            pass


def ensure_session(domain: str, *, ctx: Any = None, force: bool = False) -> RepairOutcome:
    """Best-effort, fail-open. Never raises into the caller."""
    domain = _domain(domain) or domain
    adapter = _get_adapter(domain)
    if adapter is None:
        return RepairOutcome(domain, "skipped_no_adapter")
    if not SESSION_KEEPER_ENABLED:
        return RepairOutcome(domain, "skipped_no_adapter", "session keeper disabled")

    if not force:
        in_cooldown, st = _cooldown_active(domain)
        if in_cooldown:
            return RepairOutcome(domain, "skipped_cooldown", st.get("last_blocker", ""))

    if ctx is not None:
        try:
            return _run_probe_and_repair(domain, adapter, ctx)
        except Exception as e:  # noqa: BLE001 - fail-open
            _LOG.info(f"{domain}: repair error: {e}")
            return RepairOutcome(domain, "adapter_error", f"{type(e).__name__}: {e}")

    try:
        with browser_lock(timeout=LOCK_TIMEOUT_SECONDS, holder=f"session_keeper:{domain}"):
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(CDP_URL, timeout=30000)
                try:
                    own_ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                    return _run_probe_and_repair(domain, adapter, own_ctx)
                finally:
                    try:
                        browser.close()
                    except Exception:
                        pass
    except TimeoutError:
        return RepairOutcome(domain, "lock_busy")
    except Exception as e:  # noqa: BLE001 - fail-open
        _LOG.info(f"{domain}: repair error: {e}")
        return RepairOutcome(domain, "adapter_error", f"{type(e).__name__}: {e}")


def main() -> int:
    """Manual/debugging entry point and the self-improvement patch phase's own
    verification step: `uv run python -m src.session_keeper <domain>`."""
    if len(sys.argv) < 2:
        print("usage: python -m src.session_keeper <domain>", file=sys.stderr)
        return 2
    outcome = ensure_session(sys.argv[1], force=True)
    print(f"domain={outcome.domain} outcome={outcome.outcome} detail={outcome.detail!r}")
    return 0 if outcome.outcome in ("ok", "repaired") else 1


if __name__ == "__main__":
    raise SystemExit(main())
