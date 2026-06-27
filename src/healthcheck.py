"""Periodic health checks that email you when action is needed:

  1. Low OpenRouter credit  — so applications don't silently stop.
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
CREDIT_THRESHOLD_USD = float(os.environ.get("CREDIT_THRESHOLD_USD", "2"))
SERVER_HINT = os.environ.get("SERVER_SSH", "root@your-server-ip")


def _load() -> dict:
    try:
        return json.loads(ALERTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALERTS_FILE.write_text(json.dumps(state), encoding="utf-8")


def check_credits(state: dict) -> None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("[health] no OPENROUTER_API_KEY; skipping credit check")
        return
    try:
        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
        )
        data = json.load(urllib.request.urlopen(req, timeout=20))["data"]
        remaining = float(data["total_credits"]) - float(data["total_usage"])
    except Exception as e:
        print(f"[health] credit check error: {e}")
        return
    print(f"[health] OpenRouter credit remaining: ${remaining:.2f} (threshold ${CREDIT_THRESHOLD_USD:.2f})")
    if remaining < CREDIT_THRESHOLD_USD:
        if not state.get("low_credit_sent"):
            send_alert(
                f"⚠️ Stekkies bot: low OpenRouter credit (${remaining:.2f})",
                f"Remaining OpenRouter credit is ${remaining:.2f}, below the "
                f"${CREDIT_THRESHOLD_USD:.2f} threshold.\n\n"
                f"Applications will stop once it hits $0. Top up:\n"
                f"  https://openrouter.ai/settings/credits\n",
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
