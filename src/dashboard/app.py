"""FastAPI dashboard for the Stekkies agent.

Binds 127.0.0.1; auth + TLS are provided by Caddy in front (see deploy/Caddyfile).
Read-only views + a few safe POST actions (retry / pause / resume / health check).

Run:  uv run uvicorn src.dashboard.app:app --host 127.0.0.1 --port 8000
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import apply_sessions, push_notify, store
from ..config import PROJECT_ROOT, LOG_DIR
from ..settings import settings
from . import costs, data, funnel, healthinfo, si, trajectories

BASE_DIR = Path(__file__).resolve().parent
APPLY_LOCK = PROJECT_ROOT / "state" / "apply.lock"

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    start_dashboard_warmer()  # defined below; resolved at startup, not import
    yield


app = FastAPI(title="Stekkies Agent Dashboard", lifespan=_lifespan)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
if data.SCREENSHOTS_DIR.exists():
    app.mount("/screenshots", StaticFiles(directory=str(data.SCREENSHOTS_DIR)), name="screenshots")

DASHBOARD_WARM_INTERVAL_SECONDS = settings().dashboard_warm_interval_seconds
_warm_thread_started = False
_warm_thread_lock = threading.Lock()


def _warm_dashboard_once() -> None:
    try:
        data.warm_dashboard_caches()
    except Exception:
        pass
    try:
        costs.spend_rollup(days=7)
    except Exception:
        pass
    try:
        healthinfo.warm_dashboard_caches()
    except Exception:
        pass


def _dashboard_warm_loop() -> None:
    # First pass runs immediately after startup, then periodically refreshes
    # values that page renders are allowed to serve stale.
    while True:
        _warm_dashboard_once()
        time.sleep(max(60.0, DASHBOARD_WARM_INTERVAL_SECONDS))


def start_dashboard_warmer() -> None:
    global _warm_thread_started
    with _warm_thread_lock:
        if _warm_thread_started:
            return
        _warm_thread_started = True
    threading.Thread(
        target=_dashboard_warm_loop,
        name="dashboard-cache-warmer",
        daemon=True,
    ).start()


def _action(request: Request, ok: bool, msg: str):
    """Return a toast snippet for htmx callers, JSON for programmatic ones."""
    if request.headers.get("hx-request"):
        cls = "ok" if ok else "err"
        icon = "✓" if ok else "✕"
        return HTMLResponse(
            f'<div class="toast {cls}">{icon} {msg}</div>')
    return JSONResponse({"ok": ok, "msg": msg}, status_code=200 if ok else 500)


def _active_session_context() -> tuple[list[apply_sessions.ApplySession], dict[str, apply_sessions.ApplySession]]:
    live = apply_sessions.list_sessions(live_only=True)
    by_url: dict[str, apply_sessions.ApplySession] = {}
    for session in live:
        for url in (session.stekkies_url, session.source_url):
            if url:
                by_url[url] = session
    return live, by_url

# Status -> Pico/color class for badges
STATUS_CLASS = {
    "submitted": "ok", "applied": "ok", "already_applied": "muted", "not_available": "muted",
    "not_eligible": "warn", "login_required": "warn", "blocked": "warn",
    "skipped_duplicate": "muted", "no_source_url": "muted", "no_listing_link": "muted",
    "timeout": "bad", "incomplete": "bad", "error": "bad",
}
# jinja2 leaves Environment.globals unannotated, so checkers infer an
# over-narrow value union from its defaults; widen once and assign through it.
_template_globals: dict[str, Any] = templates.env.globals
_template_globals["status_class"] = lambda s: STATUS_CLASS.get(s, "muted")
_template_globals["fmt_age"] = data.format_age
_template_globals["fmt_dur"] = data.format_duration
_template_globals["fmt_usd"] = data.format_usd
_template_globals["fmt_count"] = data.format_count


@app.get("/", response_class=HTMLResponse)
def overview(request: Request):
    subs = list(reversed(data.load_submissions()))
    stats = data.compute_stats(subs)
    spend = costs.spend_rollup(days=7)
    _live, active_by_url = _active_session_context()
    return templates.TemplateResponse(request, "index.html", {
        "stats": stats, "recent": subs[:12],
        "health": healthinfo.health(refresh_credit_if_stale=False),
        "stats_json": json.dumps(stats),
        "attention": healthinfo.attention_items(), "spend": spend,
        "kpis": data.mission_kpis(subs, spend), "active_page": "overview",
        "active_by_url": active_by_url,
    })


@app.get("/submissions", response_class=HTMLResponse)
def submissions(request: Request, status: str = "", source: str = "",
                origin: str = "", days: int = 0, page: int = 1, per: int = 50):
    all_subs = data.load_submissions()
    subs = list(reversed(all_subs))
    if status:
        subs = [s for s in subs if s.status == status]
    if source:
        subs = [s for s in subs if s.source == source]
    if origin:
        subs = [s for s in subs if s.origin == origin]
    if days > 0:
        cutoff = datetime.now() - timedelta(days=days)
        subs = [s for s in subs if (s.when or datetime.min) >= cutoff]
    total = len(subs)
    per = max(10, min(per, 200))
    pages = max(1, (total + per - 1) // per)
    page = max(1, min(page, pages))
    start = (page - 1) * per
    page_subs = subs[start:start + per]
    _live, active_by_url = _active_session_context()
    return templates.TemplateResponse(request, "submissions.html", {
        "subs": page_subs, "total": total, "page": page, "pages": pages, "per": per,
        "statuses": sorted({s.status for s in all_subs}),
        "sources": sorted({s.source for s in all_subs if s.source}),
        "origins": sorted({s.origin for s in all_subs if s.origin}),
        "f_status": status, "f_source": source, "f_origin": origin, "f_days": days,
        "active_page": "submissions",
        "active_by_url": active_by_url,
    })


@app.get("/submission/{key}", response_class=HTMLResponse)
def submission_detail(request: Request, key: str):
    sub = data.get_submission(key)
    if not sub:
        return HTMLResponse("Not found", status_code=404)
    _live, active_by_url = _active_session_context()
    return templates.TemplateResponse(request, "detail.html", {
        "sub": sub, "transcript": data.load_transcript(sub),
        "usage": costs.usage_for_submission(sub),
        "timeline": trajectories.load_timeline(sub.transcript_stem)
                    or trajectories.timeline_from_transcript(data.load_transcript(sub) or ""),
        "active_page": "submissions",
        "live_session": active_by_url.get(sub.source_url) or active_by_url.get(sub.stekkies_url),
    })


@app.get("/session/{session_id}", response_class=HTMLResponse)
def live_session(request: Request, session_id: str):
    session = apply_sessions.get_session(session_id)
    if session is None:
        return HTMLResponse("Not found", status_code=404)
    transcript, offset = data.live_transcript_snapshot(session.transcript_path)
    return templates.TemplateResponse(request, "live_session.html", {
        "session": session,
        "transcript": transcript,
        "offset": offset,
        "active_page": "submissions",
    })


@app.get("/session/{session_id}/events")
def live_session_events(session_id: str, offset: int = 0):
    if apply_sessions.get_session(session_id) is None:
        return HTMLResponse("Not found", status_code=404)

    async def events():
        position = max(0, offset)
        last_state = ""
        while True:
            session = apply_sessions.get_session(session_id)
            if session is None:
                break
            state = json.dumps({
                "status": session.status,
                "phase": session.phase,
                "outcome": session.outcome,
                "error": session.error,
                "live": session.live,
            })
            if state != last_state:
                yield f"event: status\ndata: {state}\n\n"
                last_state = state

            if session.transcript_path:
                chunk, size = await asyncio.to_thread(
                    data.live_transcript_chunk,
                    session.transcript_path,
                    position,
                )
                if size < position:
                    position = 0
                for raw_line in chunk.splitlines(keepends=True):
                    if not raw_line.endswith((b"\n", b"\r")):
                        break
                    position += len(raw_line)
                    line = data.redact(raw_line.decode("utf-8", errors="replace").rstrip("\r\n"))
                    yield f"id: {position}\nevent: message\ndata: {json.dumps(line)}\n\n"

            if not session.live:
                yield "event: complete\ndata: {}\n\n"
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health", response_class=HTMLResponse)
def health_panel(request: Request):
    return templates.TemplateResponse(request, "_health.html", {
        "health": healthinfo.health(refresh_credit_if_stale=False),
    })


@app.get("/api/stats")
def api_stats():
    return JSONResponse(data.compute_stats(data.load_submissions()))


@app.get("/funnel", response_class=HTMLResponse)
def funnel_page(request: Request, days: int = 7):
    days = days if days in (7, 30) else 7
    return templates.TemplateResponse(request, "funnel.html", {
        "days": days,
        "mail_rows": funnel.mail_funnel(days),
        "failures": funnel.failure_pareto(days),
        "incidents": funnel.incident_pareto(days=30),
        "active_page": "funnel",
    })


@app.get("/self-improvement", response_class=HTMLResponse)
def self_improvement(request: Request):
    return templates.TemplateResponse(request, "self_improvement.html", {
        "kpis": si.kpis(days=7),
        "runs": si.runs(limit=100),
        "patches": si.pending_patches(),
        "incidents": si.incidents(days=30),
        "gates": si.gates(),
        "guards": si.guard_trend(days=7),
        "playbooks": si.playbooks(),
        "lineage": si.lineage(),
        "active_page": "si",
    })


@app.get("/self-improvement/run/{name}", response_class=HTMLResponse)
def self_improvement_run(request: Request, name: str):
    log = si.run_log(name)
    if log is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "si_run.html", {
        "name": name, "log": log, "active_page": "si",
    })


@app.get("/self-improvement/patch/{name}", response_class=HTMLResponse)
def self_improvement_patch(request: Request, name: str):
    content = si.patch_content(name)
    if content is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "si_run.html", {
        "name": name, "log": content, "active_page": "si", "is_patch": True,
    })


@app.get("/playbook/{domain}", response_class=HTMLResponse)
def playbook(request: Request, domain: str):
    content = si.playbook_content(domain)
    if content is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse(request, "playbook.html", {
        "domain": domain, "content": content, "active_page": "si",
    })


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


# ---------------------------------------------------------------- web push ---
# Subscribe this browser/phone to native notifications for submissions.
# The service worker must be served from / so its scope covers the site
# (a /static/-scoped worker could not control the dashboard pages).
@app.get("/sw.js")
def service_worker():
    return FileResponse(BASE_DIR / "static" / "sw.js",
                        media_type="application/javascript")


@app.get("/push/public-key")
def push_public_key():
    return JSONResponse({"key": push_notify.public_key()})


@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    try:
        sub = await request.json()
        push_notify.add_subscription(sub, request.headers.get("user-agent", ""))
        return JSONResponse({"ok": True, "msg": "subscribed"})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    try:
        sub = await request.json()
        push_notify.remove_subscription((sub or {}).get("endpoint", ""))
        return JSONResponse({"ok": True, "msg": "unsubscribed"})
    except Exception as e:
        return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)


@app.post("/push/test")
def push_test():
    n = push_notify.send_push(
        "🔔 Test notification",
        "Web push works — you'll get one of these on every submission.")
    return JSONResponse({"ok": n > 0,
                         "msg": f"sent to {n} subscription(s)" if n else
                                "no subscriptions registered"})


@app.post("/action/pause")
def action_pause(request: Request):
    ok, msg = _systemctl("stop", "orchestrator")
    return _action(request, ok, msg or "orchestrator stopped")


@app.post("/action/resume")
def action_resume(request: Request):
    ok, msg = _systemctl("start", "orchestrator")
    return _action(request, ok, msg or "orchestrator started")


@app.post("/action/healthcheck")
def action_healthcheck(request: Request):
    ok, msg = _systemctl("start", "healthcheck.service")
    return _action(request, ok, msg or "health check triggered")



@app.post("/action/retry")
def action_retry(request: Request, url: str = ""):
    if not url or "stekkies.com" not in url:
        return _action(request, False, "invalid url")
    # Refuse before touching dedup state if another dashboard retry owns the
    # launch lock.  A rejected click must not make a listing retryable later.
    if APPLY_LOCK.exists():
        return _action(request, False, "an apply is already running; try again shortly")
    # Create the observable identity before making the listing retryable.  If
    # state creation fails, the duplicate guard remains intact.
    session = apply_sessions.create_session(stekkies_url=url, retry=True)
    # Remove from dedup so the listing is re-attempted.
    try:
        store.delete_processed(url)
    except Exception as e:
        apply_sessions.finish_session(
            session.id, error=f"dedup edit failed: {type(e).__name__}: {e}")
        return _action(request, False, f"dedup edit failed: {e}")
    # Serialize with a lock so we never share the browser with the live watcher's apply.
    try:
        APPLY_LOCK.write_text("retry", encoding="utf-8")
        # Wrapper clears the lock when the apply finishes (fire-and-forget).
        cmd = (
            f"{shlex.quote(sys.executable)} -m src.orchestrator --once "
            f"{shlex.quote(url)} --session-id {shlex.quote(session.id)}; "
            f"rm -f {shlex.quote(str(APPLY_LOCK))}"
        )
        with open(LOG_DIR / "retry.log", "ab") as log:
            # Popen dups the fd at spawn; the child keeps writing after we close.
            subprocess.Popen(
                ["bash", "-c", cmd],
                cwd=str(PROJECT_ROOT), env={**os.environ}, stdout=log, stderr=log,
                start_new_session=True,
            )
    except Exception as e:
        APPLY_LOCK.unlink(missing_ok=True)
        apply_sessions.finish_session(
            session.id, error=f"{type(e).__name__}: {e}")
        return _action(request, False, f"launch failed: {e}")
    location = f"/session/{session.id}"
    if request.headers.get("hx-request"):
        return HTMLResponse("", headers={"HX-Redirect": location})
    return JSONResponse({"ok": True, "msg": "retry launched", "session_url": location})


@app.post("/action/gate-delete")
def action_gate_delete(request: Request, domain: str = "", kind: str = ""):
    from ..known_gates import remove_gate
    try:
        msg = remove_gate(domain, kind)
        data_cache_bust()
        return _action(request, True, msg)
    except Exception as e:
        return _action(request, False, str(e))


def data_cache_bust() -> None:
    """Drop memoized derived state so a mutating action is reflected at once."""
    from . import cache
    cache.clear()


@app.get("/partial/attention", response_class=HTMLResponse)
def attention_panel(request: Request):
    return templates.TemplateResponse(request, "_attention.html", {
        "attention": healthinfo.attention_items(),
    })


@app.get("/partial/live-sessions", response_class=HTMLResponse)
def live_sessions_panel(request: Request):
    live, _by_url = _active_session_context()
    return templates.TemplateResponse(request, "_live_sessions.html", {
        "live_sessions": live,
    })
