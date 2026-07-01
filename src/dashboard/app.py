"""FastAPI dashboard for the Stekkies agent.

Binds 127.0.0.1; auth + TLS are provided by Caddy in front (see deploy/Caddyfile).
Read-only views + a few safe POST actions (retry / pause / resume / health check).

Run:  uv run uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import PROJECT_ROOT, LOG_DIR
from . import data

BASE_DIR = Path(__file__).resolve().parent
PROCESSED_FILE = PROJECT_ROOT / "state" / "processed_listings.jsonl"
APPLY_LOCK = PROJECT_ROOT / "state" / "apply.lock"

app = FastAPI(title="Stekkies Agent Dashboard")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
if data.SCREENSHOTS_DIR.exists():
    app.mount("/screenshots", StaticFiles(directory=str(data.SCREENSHOTS_DIR)), name="screenshots")

# Status -> Pico/color class for badges
STATUS_CLASS = {
    "submitted": "ok", "applied": "ok", "already_applied": "muted", "not_available": "muted",
    "not_eligible": "warn", "login_required": "warn", "blocked": "warn",
    "skipped_duplicate": "muted", "no_source_url": "muted", "no_listing_link": "muted",
    "timeout": "bad", "incomplete": "bad", "error": "bad",
}
templates.env.globals["status_class"] = lambda s: STATUS_CLASS.get(s, "muted")
templates.env.globals["fmt_delta"] = data.format_delta


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    subs = list(reversed(data.load_submissions()))
    mail_events = data.load_mail_events()
    stats = data.compute_stats(subs)
    race = data.race_report(subs, mail_events)
    return templates.TemplateResponse(request, "index.html", {
        "stats": stats, "recent": subs[:12], "race": race,
        "mail_events": mail_events[:10],
        "health": data.health(), "stats_json": json.dumps(stats),
    })


@app.get("/submissions", response_class=HTMLResponse)
def submissions(request: Request, status: str = "", source: str = "", origin: str = ""):
    subs = list(reversed(data.load_submissions()))
    if status:
        subs = [s for s in subs if s.status == status]
    if source:
        subs = [s for s in subs if s.source == source]
    if origin:
        subs = [s for s in subs if s.origin == origin]
    all_subs = data.load_submissions()
    mail_events = data.load_mail_events()
    race = data.race_report(all_subs, mail_events)
    return templates.TemplateResponse(request, "submissions.html", {
        "subs": subs,
        "statuses": sorted({s.status for s in all_subs}),
        "sources": sorted({s.source for s in all_subs if s.source}),
        "origins": sorted({s.origin for s in all_subs if s.origin}),
        "f_status": status, "f_source": source, "f_origin": origin,
        "race_by_url": {r.source_url: r for r in race["rows"]},
    })


@app.get("/submission/{sub_id}", response_class=HTMLResponse)
def submission_detail(request: Request, sub_id: int):
    sub = data.get_submission(sub_id)
    if not sub:
        return HTMLResponse("Not found", status_code=404)
    race = data.race_report(data.load_submissions(), data.load_mail_events())
    race_by_url = {r.source_url: r for r in race["rows"]}
    return templates.TemplateResponse(request, "detail.html", {
        "sub": sub, "transcript": data.load_transcript(sub),
        "race": race_by_url.get(sub.source_url),
    })


@app.get("/health", response_class=HTMLResponse)
def health_panel(request: Request):
    return templates.TemplateResponse(request, "_health.html", {
        "health": data.health(),
    })


@app.get("/api/stats")
def api_stats():
    return JSONResponse(data.compute_stats(data.load_submissions()))


# --------------------------------------------------------------------------- #
# Safe actions (behind Caddy basic-auth; same-origin POSTs)
# --------------------------------------------------------------------------- #
def _systemctl(*args: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(["sudo", "-n", "systemctl", *args],
                           capture_output=True, text=True, timeout=15)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except Exception as e:
        return False, str(e)


@app.post("/action/pause")
def action_pause():
    ok, msg = _systemctl("stop", "orchestrator")
    return JSONResponse({"ok": ok, "msg": msg or "orchestrator stopped"})


@app.post("/action/resume")
def action_resume():
    ok, msg = _systemctl("start", "orchestrator")
    return JSONResponse({"ok": ok, "msg": msg or "orchestrator started"})


@app.post("/action/poller-pause")
def action_poller_pause():
    ok, msg = _systemctl("stop", "poller")
    return JSONResponse({"ok": ok, "msg": msg or "poller stopped"})


@app.post("/action/poller-resume")
def action_poller_resume():
    ok, msg = _systemctl("start", "poller")
    return JSONResponse({"ok": ok, "msg": msg or "poller started"})


@app.post("/action/healthcheck")
def action_healthcheck():
    ok, msg = _systemctl("start", "healthcheck.service")
    return JSONResponse({"ok": ok, "msg": msg or "health check triggered"})


@app.post("/action/refresh-mail-index")
def action_refresh_mail_index():
    try:
        events = data.load_mail_events(force=True)
        return JSONResponse({"ok": True, "msg": f"indexed {len(events)} mail signals"})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=500)


@app.post("/action/retry")
def action_retry(url: str = ""):
    if not url or "stekkies.com" not in url:
        return JSONResponse({"ok": False, "msg": "invalid url"}, status_code=400)
    # Remove from dedup so the listing is re-attempted.
    try:
        if PROCESSED_FILE.exists():
            kept = [ln for ln in PROCESSED_FILE.read_text(encoding="utf-8").splitlines()
                    if url not in ln]
            PROCESSED_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except Exception as e:
        return JSONResponse({"ok": False, "msg": f"dedup edit failed: {e}"}, status_code=500)
    # Serialize with a lock so we never share the browser with the live watcher's apply.
    if APPLY_LOCK.exists():
        return JSONResponse({"ok": False, "msg": "an apply is already running; try again shortly"}, status_code=409)
    try:
        APPLY_LOCK.write_text("retry", encoding="utf-8")
        log = open(LOG_DIR / "retry.log", "ab")
        # Wrapper clears the lock when the apply finishes (fire-and-forget).
        cmd = (
            f"{shlex.quote(sys.executable)} -m src.orchestrator --once "
            f"{shlex.quote(url)}; rm -f {shlex.quote(str(APPLY_LOCK))}"
        )
        subprocess.Popen(
            ["bash", "-c", cmd],
            cwd=str(PROJECT_ROOT), env={**os.environ}, stdout=log, stderr=log,
            start_new_session=True,
        )
    except Exception as e:
        APPLY_LOCK.unlink(missing_ok=True)
        return JSONResponse({"ok": False, "msg": f"launch failed: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "msg": "retry launched (check submissions in a minute)"})
