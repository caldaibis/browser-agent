"""Periodic health checks that email you when action is needed:

  1. Low DeepSeek credit  — so applications don't silently stop.
  2. Stekkies session expired — so you can re-login (the server can't read new
     listings without it). Source-site logins are surfaced reactively per-listing
     via the login_required / no_source_url outcome emails.

Alerts are de-duped via state/alerts.json: one email when a problem appears, and
it re-arms once the problem clears. Run periodically (systemd timer):

  python -m src.healthcheck
"""
import json
import os
import urllib.request
from pathlib import Path

from .config import PROJECT_ROOT, CDP_URL
from .notify import send_alert

ALERTS_FILE = PROJECT_ROOT / "state" / "alerts.json"
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
CREDIT_CURRENCY = os.environ.get("CREDIT_CURRENCY", "USD").upper()
CREDIT_THRESHOLD = float(os.environ.get("CREDIT_THRESHOLD", os.environ.get("CREDIT_THRESHOLD_USD", "2")))
CREDIT_THRESHOLD_USD = CREDIT_THRESHOLD  # backward-compatible import for older code
SERVER_HINT = os.environ.get("SERVER_SSH", "root@your-server-ip")


def _load() -> dict:
    try:
        return json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERTS_FILE.write_text(json.dumps(state), encoding="utf-8")


def remaining_credit() -> tuple[float, str] | None:
    """Remaining DeepSeek credit, or None if unavailable.

    Returns ``(amount, currency)`` for the configured CREDIT_CURRENCY when
    present, otherwise the first balance returned by DeepSeek.
    """
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return None
    try:
        req = urllib.request.Request(
            f"{DEEPSEEK_BASE_URL.rstrip('/')}/user/balance",
            headers={"Authorization": f"Bearer {key}"},
        )
        data = json.load(urllib.request.urlopen(req, timeout=20))
        balances = data.get("balance_infos") or []
        if not balances:
            return None
        selected = next(
            (b for b in balances if str(b.get("currency", "")).upper() == CREDIT_CURRENCY),
            balances[0],
        )
        return float(selected["total_balance"]), str(selected["currency"]).upper()
    except Exception as e:
        print(f"[health] credit check error: {e}")
        return None


def check_credits(state: dict) -> None:
    credit = remaining_credit()
    if credit is None:
        print("[health] credit unavailable; skipping credit check")
        return
    remaining, currency = credit
    print(f"[health] DeepSeek credit remaining: {currency} {remaining:.2f} (threshold {CREDIT_THRESHOLD:.2f})")
    if remaining < CREDIT_THRESHOLD_USD:
        if not state.get("low_credit_sent"):
            send_alert(
                f"⚠️ Stekkies bot: low DeepSeek credit ({currency} {remaining:.2f})",
                f"Remaining DeepSeek credit is {currency} {remaining:.2f}, below the "
                f"{CREDIT_THRESHOLD:.2f} threshold.\n\n"
                f"Applications will stop once it hits 0. Top up:\n"
                f"  https://platform.deepseek.com/top_up\n",
            )
            state["low_credit_sent"] = True
    else:
        state["low_credit_sent"] = False


def check_stekkies_login(state: dict) -> None:
    from playwright.sync_api import sync_playwright

    logged_out = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto("https://www.stekkies.com/en/profiles/matches/",
                          wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                has_pw = page.locator("input[type=password]").count() > 0
                logged_out = ("/login" in page.url) or has_pw
            finally:
                page.close()
                browser.close()
    except Exception as e:
        print(f"[health] stekkies login check error: {e}")
        return
    print(f"[health] stekkies logged_out={logged_out}")
    if logged_out:
        if not state.get("stekkies_logout_sent"):
            send_alert(
                "\U0001f511 Stekkies bot: session expired — re-login needed",
                "The server's Stekkies session looks logged out, so new listings "
                "cannot be read. Re-login via VNC:\n\n"
                f"  ssh {SERVER_HINT} 'systemctl start vnc.service'\n"
                f"  ssh -L 5900:localhost:5900 {SERVER_HINT}\n"
                "  # connect a VNC viewer to localhost:5900, log into Stekkies "
                "(and Google), then:\n"
                f"  ssh {SERVER_HINT} 'systemctl stop vnc.service'\n",
            )
            state["stekkies_logout_sent"] = True
    else:
        state["stekkies_logout_sent"] = False


def main() -> int:
    state = _load()
    check_credits(state)
    check_stekkies_login(state)
    _save(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
