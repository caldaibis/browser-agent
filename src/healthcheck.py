"""Periodic health checks that alert (web push + email) when action is needed:

  1. Low DeepSeek credit  — so applications don't silently stop.
  2. Session expired on Stekkies or a top apply site — so you can re-login
     before listings are lost to login_required outcomes.
  3. A pipeline systemd service is not running — a crash-looping orchestrator
     means every alert mail is being silently dropped (happened 04..07-07-2026:
     Gmail token revoked, 1136 restarts, 3 days of lost listings, and no email
     alert could reach the user because email needs that same token; push +
     this check close that hole).
  4. The self-improvement layer itself failing repeatedly — 27 identical
     crashes over 01-07-2026 went unnoticed as a pattern because each one
     looked like a single bad run. Same lesson as #3 applied one level up:
     the thing that fixes failures needs its own watcher.

Also sends the weekly outcome digest (src/digest.py) piggybacked on this
timer — no extra systemd unit needed.

Alerts are de-duped via state/alerts.json: one alert when a problem appears,
re-armed once the problem clears. Optionally pings a dead-man's-switch URL
(HEALTHCHECK_PING_URL, e.g. healthchecks.io) at the end of every run so a
dead VPS/timer also gets noticed — by the ping *stopping*.
Run periodically (systemd timer):

  python -m src.healthcheck
"""
import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime

from . import eventlog
from .config import LOG_DIR, PROJECT_ROOT, CDP_URL
from .notify import send_alert
from .poller.browser_lock import browser_lock
from .playwright_mcp_runtime import initialize_check, runtime_check
from .settings import settings
from .eventlog import get_logger

_LOG = get_logger("health")

ALERTS_FILE = PROJECT_ROOT / "state" / "alerts.json"
DEEPSEEK_BASE_URL = settings().deepseek_base_url
CREDIT_CURRENCY = settings().credit_currency
CREDIT_THRESHOLD = settings().credit_threshold
CREDIT_THRESHOLD_USD = CREDIT_THRESHOLD  # backward-compatible import for older code
SERVER_HINT = settings().server_ssh_hint

# systemd units that must be active for the pipeline to work at all.
SERVICES = settings().healthcheck_services

# Dead-man's-switch: GET this URL at the end of every healthcheck run (e.g. a
# healthchecks.io check). The monitoring service alerts when pings STOP —
# covering the failure class nothing on this box can report: the box itself.
PING_URL = settings().healthcheck_ping_url

# Sites whose logged-in session the apply agent depends on. Each probe opens
# an account-only page in the REAL shared browser (so it sees the profile's
# actual cookies) and applies the logged-out heuristic below. Extend via
# HEALTHCHECK_SITE_PROBES='{"name": "https://account-url", ...}'.
# 6 listings were lost to login_required outcomes (huurwoningen.nl x4,
# huurexpert.nl x2) before this existed — each one a prime, mail-alerted
# listing burned on an expired session nothing was watching.
SITE_PROBES: dict[str, str] = {
    "stekkies": "https://www.stekkies.com/en/profiles/matches/",
    "huurwoningen.nl": "https://www.huurwoningen.nl/mijn-huurwoningen/",
    "kamernet.nl": "https://kamernet.nl/en/my-kamernet",
}
try:
    SITE_PROBES.update(json.loads(settings().healthcheck_site_probes_json))
except (json.JSONDecodeError, TypeError):
    _LOG.info("ignoring malformed HEALTHCHECK_SITE_PROBES")


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
    key = settings().deepseek_api_key
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
        _LOG.info(f"credit check error: {e}")
        return None


def check_credits(state: dict) -> None:
    credit = remaining_credit()
    if credit is None:
        _LOG.info("credit unavailable; skipping credit check")
        return
    remaining, currency = credit
    _LOG.info(f"DeepSeek credit remaining: {currency} {remaining:.2f} (threshold {CREDIT_THRESHOLD:.2f})")
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


def check_services(state: dict) -> None:
    """Alert when a pipeline systemd unit is not active (crash loop, dead)."""
    # The canonical "is systemd actually running here" check — without it a
    # non-systemd host (local WSL dev) reads the bus failure as "everything
    # is down" and fires alerts for services that were never supposed to run.
    if not os.path.isdir("/run/systemd/system"):
        _LOG.info("not a systemd host; skipping service checks")
        return
    for svc in SERVICES:
        try:
            rc = subprocess.run(
                ["systemctl", "is-active", "--quiet", svc],
                timeout=15,
            ).returncode
        except FileNotFoundError:
            _LOG.info("systemctl not available; skipping service checks")
            return
        except Exception as e:  # noqa: BLE001 - one check must not kill the run
            _LOG.info(f"service check error for {svc}: {e}")
            continue
        key = f"service_down_sent:{svc}"
        if rc != 0:
            _LOG.info(f"service {svc} NOT active (rc={rc})")
            if not state.get(key):
                send_alert(
                    f"🚨 Stekkies bot: service {svc} is DOWN",
                    f"systemd reports `{svc}` is not active. While it is down "
                    "this part of the pipeline processes nothing.\n\n"
                    f"  ssh {SERVER_HINT} 'systemctl status {svc}'\n"
                    f"  ssh {SERVER_HINT} 'journalctl -u {svc} -n 50'\n\n"
                    "If the orchestrator is failing with invalid_grant, the "
                    "Gmail token died — run `just reauth` from the repo.\n",
                )
                state[key] = True
        else:
            state[key] = False


def check_playwright_mcp(state: dict) -> None:
    """Functional runtime probe: active units can still have a dead MCP."""
    ok, detail = runtime_check()
    if ok:
        ok, detail = initialize_check(CDP_URL)
    _LOG.info(f"Playwright MCP runtime ok={ok}: {detail}")
    key = "playwright_mcp_runtime_sent"
    if not ok:
        if not state.get(key):
            send_alert(
                "🚨 Stekkies bot: Playwright MCP cannot start",
                f"The browser automation runtime failed its startup probe:\n\n"
                f"{detail}\n\nApplications will fail before their first turn. "
                "Run deploy/ensure-self-improvement.sh to enforce Node 20+ "
                "and reinstall the pinned MCP package.",
            )
            state[key] = True
    else:
        state[key] = False


def _logged_out_heuristic(page) -> bool:
    """Shared logged-out signal: the account page bounced to a login flow or
    is showing a password prompt."""
    url = page.url.lower()
    if any(m in url for m in ("/login", "inloggen", "aanmelden", "/signin")):
        return True
    try:
        return page.locator("input[type=password]").count() > 0
    except Exception:
        return False


def check_site_logins(state: dict) -> None:
    """Probe each SITE_PROBES account page in the real shared browser.

    Takes the browser lock with a SHORT timeout: when an apply run is in
    flight the checks are simply skipped until the next timer tick — a
    healthcheck must never queue behind (or interleave tabs with) a live
    submission. (The pre-existing Stekkies check drove the shared browser
    without the lock at all; this also fixes that.)
    """
    from playwright.sync_api import sync_playwright

    results: dict[str, bool] = {}
    try:
        with browser_lock(timeout=60, holder="healthcheck"):
            with sync_playwright() as p:
                browser = p.chromium.connect_over_cdp(CDP_URL)
                try:
                    ctx = (browser.contexts[0] if browser.contexts
                           else browser.new_context())
                    for name, probe_url in SITE_PROBES.items():
                        page = ctx.new_page()
                        try:
                            page.goto(probe_url, wait_until="domcontentloaded",
                                      timeout=30000)
                            page.wait_for_timeout(1500)
                            results[name] = _logged_out_heuristic(page)
                        except Exception as e:  # noqa: BLE001 - per-site best effort
                            _LOG.info(f"login probe error for {name}: {e}")
                        finally:
                            page.close()
                finally:
                    browser.close()
    except TimeoutError:
        _LOG.info("browser busy (apply in progress?); skipping login checks")
        return
    except Exception as e:
        _LOG.info(f"login checks error: {e}")
        return

    for name, logged_out in results.items():
        _LOG.info(f"{name} logged_out={logged_out}")
        key = f"logout_sent:{name}"
        if logged_out:
            if not state.get(key):
                send_alert(
                    f"🔑 Stekkies bot: {name} session expired — re-login needed",
                    f"The server's {name} session looks logged out. Until you "
                    "re-login, applications on this site fail with "
                    "login_required (mail-alerted listings included).\n"
                    "Re-login via VNC:\n\n"
                    f"  ssh {SERVER_HINT} 'systemctl start vnc.service'\n"
                    f"  ssh -L 5900:localhost:5900 {SERVER_HINT}\n"
                    f"  # connect a VNC viewer to localhost:5900, log into "
                    f"{name} (credentials: state/sources_credentials.json), then:\n"
                    f"  ssh {SERVER_HINT} 'systemctl stop vnc.service'\n",
                )
                state[key] = True
        else:
            state[key] = False


# Alert on a high rolling failure ratio, abandoned runs, or durable queue
# failures. Skips/dedup records are neither success nor failure.
SELF_IMPROVEMENT_HEALTH_WINDOW = settings().self_improvement_health_window
SELF_IMPROVEMENT_HEALTH_FAILURE_RATIO = settings().self_improvement_health_failure_ratio
SELF_IMPROVEMENT_ORPHAN_SECONDS = settings().self_improvement_orphan_seconds
_SI_FAILURE_ACTIONS = {"error", "fix_failed", "timeout", "incomplete"}


def check_self_improvement(state: dict) -> None:
    runs: list[bool] = []  # True = failed
    starts: dict[str, datetime] = {}
    terminals: set[str] = set()
    abandoned_recent = 0
    try:
        with (LOG_DIR / "self_improvement.jsonl").open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = rec.get("event")
                run_id = str(rec.get("run_id") or "")
                if event == "run_started" and run_id:
                    ts = eventlog.parse_ts(rec.get("ts"))
                    if ts is not None:
                        starts[run_id] = ts
                elif event == "run_finished" and run_id:
                    terminals.add(run_id)
                elif event == "run_abandoned":
                    abandoned_ts = eventlog.parse_ts(rec.get("ts"))
                    if (abandoned_ts is not None
                            and (datetime.now() - abandoned_ts).total_seconds() < 86400):
                        abandoned_recent += 1
                if event == "error":
                    runs.append(True)
                elif event == "done":
                    runs.append(str(rec.get("action") or "") in _SI_FAILURE_ACTIONS)
    except OSError:
        return
    recent = runs[-SELF_IMPROVEMENT_HEALTH_WINDOW:]
    failure_ratio = sum(recent) / len(recent) if recent else 0.0
    unhealthy_ratio = (
        len(recent) >= SELF_IMPROVEMENT_HEALTH_WINDOW
        and failure_ratio >= SELF_IMPROVEMENT_HEALTH_FAILURE_RATIO
    )
    now = datetime.now()
    orphans = [
        run_id for run_id, started in starts.items()
        if run_id not in terminals
        and (now - started).total_seconds() >= SELF_IMPROVEMENT_ORPHAN_SECONDS
    ]
    from .self_improvement_queue import queue_counts
    queue_state = queue_counts()
    unhealthy = (unhealthy_ratio or bool(orphans) or abandoned_recent > 0
                 or queue_state["failed"] > 0)
    _LOG.info(f"self-improvement recent failures: "
          f"{sum(recent)}/{len(recent)} ratio={failure_ratio:.2f}; "
          f"orphans={len(orphans)} abandoned={abandoned_recent} queue={queue_state}")
    if unhealthy:
        if not state.get("si_failing_sent"):
            send_alert(
                "🚨 Stekkies bot: self-improvement keeps failing",
                f"Recent failure rate is {sum(recent)}/{len(recent)} "
                f"(threshold {SELF_IMPROVEMENT_HEALTH_FAILURE_RATIO:.0%}); "
                f"orphaned runs={len(orphans)}, recovered abandonments={abandoned_recent}; "
                f"queue={queue_state}. The layer that is "
                "supposed to repair failures is itself broken or blocked "
                "(dead proxy? read-only deploy key? see "
                "state/pending_patches/ for unlanded fixes).\n\n"
                f"  ssh {SERVER_HINT} 'tail -n 20 ~deploy/browser-agent/logs/self_improvement.jsonl'\n",
            )
            state["si_failing_sent"] = True
    else:
        state["si_failing_sent"] = False


# Weekly outcome digest, piggybacked on the healthcheck timer (30 min) so it
# needs no extra systemd unit. 0 disables.
DIGEST_INTERVAL_DAYS = settings().digest_interval_days


def maybe_send_digest(state: dict) -> None:
    if DIGEST_INTERVAL_DAYS <= 0:
        return
    last = float(state.get("digest_sent_ts") or 0)
    if time.time() - last < DIGEST_INTERVAL_DAYS * 86400:
        return
    try:
        from .digest import build_digest

        body = build_digest(days=DIGEST_INTERVAL_DAYS)
    except Exception as e:  # noqa: BLE001 - digest must never fail the healthcheck
        _LOG.info(f"digest build failed: {e}")
        return
    send_alert("📊 Stekkies bot: weekly digest", body)
    state["digest_sent_ts"] = time.time()
    _LOG.info("weekly digest sent")


def ping_deadman() -> None:
    """Tell the external dead-man's switch this healthcheck ran. Its absence —
    box down, timer broken, repo wedged — is what actually raises the alarm,
    on infrastructure that does not share fate with this VPS."""
    if not PING_URL:
        return
    try:
        urllib.request.urlopen(PING_URL, timeout=10)
        _LOG.info("dead-man ping sent")
    except Exception as e:  # noqa: BLE001 - the ping must never fail the run
        _LOG.info(f"dead-man ping failed: {e}")


def main() -> int:
    state = _load()
    check_credits(state)
    check_services(state)
    check_playwright_mcp(state)
    check_site_logins(state)
    check_self_improvement(state)
    maybe_send_digest(state)
    _save(state)
    ping_deadman()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
