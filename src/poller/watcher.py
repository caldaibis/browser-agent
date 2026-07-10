"""Active listing poller — watch source sites directly and feed the applier.

Runs every enabled site on its own cadence+jitter concurrently. Per poll:
  fetch (tier 1/2 httpx, tier 3 rendered tab) -> block-detect -> parse ->
  dedup (canonical URL) -> deterministic filter -> LLM judgment -> enqueue.

A single applier task drains the queue and runs the EXISTING apply pipeline
(apply.apply, which itself takes the exclusive browser lock), so submissions are
serialized and never race the Stekkies orchestrator.

Run:
  python -m src.poller.watcher                 # watch all enabled sites
  python -m src.poller.watcher --once NAME     # one poll of one site, no apply
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import json
import multiprocessing
import os
import queue as thread_queue
import random
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import httpx

from .. import eventlog, store
from ..apply_priority import priority_pending
from ..config import LOG_DIR
from ..models import ProcessedRecord
from ..settings import settings
from ..listing_context import fetch_context
from ..notify import send_alert, send_alert_dedup, send_status_email
from . import filters, judge
from .browser_lock import browser_lock
from .dedup import SeenStore
from .fetch import Blocked, fetch
from .models import RawListing, SiteConfig
from .parsers import parse_jsonld
from .registry import by_name, enabled_sites

POLL_LOG = LOG_DIR / "poller.jsonl"
MAIL_SUMMARY_LOG = LOG_DIR / "mail_summary.jsonl"
ACTIVITY_LOG = LOG_DIR / "activity.log"
ZERO_YIELD_DIR = LOG_DIR / "poller_zero_yield"

# Tier-3 render settle time (ms) after DOM load, for the listing JS to populate.
SETTLE_MS = settings().poll_tier3_settle_ms

# Size of the event loop's default thread executor. The default
# (min(32, cpus+4) — 8 threads on a 4-vCPU VPS) is far too small here:
# every tier-3 render and every apply parks a thread in asyncio.to_thread
# waiting on the browser flock, and asyncio resolves DNS (loop.getaddrinfo)
# on that SAME executor. With ~13 tier-3 watchers a full executor starves
# DNS, so every pending httpx connect times out AT ONCE — observed as 10k+
# simultaneous ConnectTimeout poll_errors per day across all tier-2 sites
# (07-07-2026), i.e. ~80% of tier-2 polls silently lost.
EXECUTOR_THREADS = settings().poll_executor_threads

# A speculative tier-3 poll must not queue behind a long apply for the shared
# browser: skip the poll and try again next cadence (also frees its executor
# thread quickly — see EXECUTOR_THREADS).
TIER3_LOCK_TIMEOUT = settings().poll_tier3_lock_timeout

# Hard wall-clock cap for one shared-browser tier-3 render, including lock
# acquisition and Playwright teardown. This intentionally sits well below the
# browser-lock wait alert threshold (300s by default): if a speculative poll
# wedges, kill it before any apply spends five minutes waiting behind it.
TIER3_RENDER_TIMEOUT = settings().poll_tier3_render_timeout

# Cleanup should never dominate the lock-hold time. The parent process still
# has the hard TIER3_RENDER_TIMEOUT kill switch if Playwright ignores this.
TIER3_CLOSE_TIMEOUT = settings().poll_tier3_close_timeout

# Consecutive zero-listing polls before suspecting a silently-broken parser:
# a parser that matches nothing looks exactly like "no new listings", forever.
ZERO_YIELD_ALERT_POLLS = settings().poll_zero_yield_alert_polls

# Backoff schedule (seconds) applied on consecutive blocks, capped.
_BACKOFF = [60, 300, 900, 1800, 3600]

# One attempt per listing — no automatic retries. A retry re-runs the exact
# same prompt against the exact same page (nothing carries over from the failed
# attempt), so it is an identical coin flip at full LLM cost; in practice a
# listing that fails non-terminally once fails the same way again (Hof van
# Oslo: 15+ retried runs at ~5M tokens each on 01-07-2026, all `incomplete`).
# Every completed agent run therefore consumes the listing, whatever the
# outcome. The one exception is outcome "yielded": the run was aborted to hand
# the browser to a priority mail apply (see ..apply_priority) — that says
# nothing about the listing, so it is requeued untouched.


def _log(event: str, **kw) -> None:
    eventlog.log_event(POLL_LOG, event, echo="poll", **kw)


def _summary(**kw) -> dict:
    return eventlog.record(MAIL_SUMMARY_LOG, trigger="poller", **kw)


def _activity(message: str) -> None:
    eventlog.activity(message, echo="poll")


async def _write_zero_yield_diagnostic(client: httpx.AsyncClient, site: SiteConfig) -> str:
    """Capture a small sample of what the parser saw when a site yields zero.

    Alerts are useful, but an HTML sample is what makes the next parser fix
    fast. Best-effort and side-effect-only in logs/.
    """
    ZERO_YIELD_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in ".-" else "-" for c in site.name)
    path = ZERO_YIELD_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe}.html"
    try:
        if site.tier == 3 and site.own_browser:
            html = await _render_own_browser(site)
        elif site.tier == 3:
            html = await _render_tier3(site)
        else:
            result = await fetch(client, site)
            html = json.dumps(result.json, ensure_ascii=False, indent=2) \
                if site.tier == 1 else result.text
        await asyncio.to_thread(
            Path(path).write_text, (html or "")[:250_000], encoding="utf-8")
        return str(path)
    except Exception as e:  # noqa: BLE001 - diagnostics must not break polling
        _log("zero_yield_diagnostic_failed", site=site.name,
             error=f"{type(e).__name__}: {e}")
        return ""


def _remember_processed(listing: RawListing, outcome: str, resolved_url: str = "") -> None:
    rec = {
        "trigger": "poller",
        "source_url": listing.source_url,
        "source": listing.source_name,
        "detected_by": listing.detected_by or listing.source_name,
        "address": listing.address,
        "outcome": outcome,
    }
    # The real external destination the agent reached, when different from
    # listing.source_url (e.g. an aggregator page redirected in-browser to
    # the actual landlord site) -- an extra dedup key so the Stekkies-mail
    # path (which records the final URL directly) recognizes this listing
    # as already handled even though it was discovered under a different
    # URL. See poller.dedup.known_processed_urls.
    if resolved_url:
        rec["resolved_url"] = resolved_url
    try:
        store.record_processed(ProcessedRecord.from_json(rec))
    except Exception as e:
        _log("store_write_failed", error=f"{type(e).__name__}: {e}")


async def _render_tier3(site: SiteConfig) -> str:
    """Tier 3: open a real tab over CDP (under the browser lock) and return its
    HTML. Used only for sites that defeat httpx (JS-gated / login-walled list)."""
    if priority_pending():
        raise TimeoutError("priority apply pending; skipping tier-3 render")
    return await asyncio.to_thread(_render_tier3_process, site.name, site.list_url)


async def _render_tier3_page(list_url: str) -> str:
    """Render a tier-3 list page in the shared CDP browser.

    Runs inside a disposable child process; keep all cleanup bounded because
    the parent will terminate the process if this coroutine or Playwright's
    teardown wedges while holding the browser flock.
    """
    from playwright.async_api import async_playwright
    from ..config import CDP_URL

    pw = await async_playwright().start()
    browser = None
    page = None
    try:
        # Explicit connect timeout: a hanging CDP connect during a network
        # disruption was what wedged a tier-3 render inside the browser lock on
        # 03-07-2026 (diagnosed on-box by the self-improvement agent).
        browser = await pw.chromium.connect_over_cdp(CDP_URL, timeout=30000)
        ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = await ctx.new_page()
        # domcontentloaded + a fixed settle beats "networkidle": many listing
        # SPAs hold connections open and never go idle.
        await page.goto(list_url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(SETTLE_MS)
        return await page.content()
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(page.close(), timeout=TIER3_CLOSE_TIMEOUT)
        if browser is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(browser.close(), timeout=TIER3_CLOSE_TIMEOUT)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(pw.stop(), timeout=TIER3_CLOSE_TIMEOUT)


def _render_tier3_child(site_name: str, list_url: str, output_path: str,
                        result_queue) -> None:
    try:
        if priority_pending():
            raise TimeoutError("priority apply pending; skipping tier-3 render")
        async def _bounded() -> str:
            async with asyncio.timeout(TIER3_RENDER_TIMEOUT):
                return await _render_tier3_page(list_url)

        def _locked() -> str:
            with browser_lock(timeout=TIER3_LOCK_TIMEOUT, holder=f"tier3:{site_name}"):
                return asyncio.run(_bounded())

        html = _locked()
        Path(output_path).write_text(html, encoding="utf-8")
        result_queue.put(("ok", "", ""))
    except BaseException as e:  # noqa: BLE001 - report child failure to parent
        result_queue.put(("err", type(e).__name__, str(e)))


def _render_tier3_process(site_name: str, list_url: str) -> str:
    """Run shared-browser tier-3 rendering in a killable child process.

    A thread-level timeout cannot safely interrupt a wedged Playwright teardown
    after it has acquired the browser flock. A child-process boundary can: if
    the render exceeds TIER3_RENDER_TIMEOUT, terminating the process releases
    the OS file lock and lets pending applies proceed.
    """
    if priority_pending():
        raise TimeoutError("priority apply pending; skipping tier-3 render")

    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    tmp_fd, output_path = tempfile.mkstemp(
        prefix=f"tier3_{site_name.replace('.', '_')}_", suffix=".html")
    os.close(tmp_fd)
    proc = ctx.Process(
        target=_render_tier3_child,
        args=(site_name, list_url, output_path, result_queue),
        name=f"tier3-render:{site_name}",
    )
    proc.start()
    try:
        proc.join(TIER3_RENDER_TIMEOUT)
        if proc.is_alive():
            proc.terminate()
            proc.join(5)
            if proc.is_alive():
                proc.kill()
                proc.join(5)
            raise TimeoutError(
                f"tier-3 render for {site_name} exceeded "
                f"{TIER3_RENDER_TIMEOUT:.0f}s")
        try:
            status, kind, message = result_queue.get_nowait()
        except thread_queue.Empty:
            raise RuntimeError(
                f"tier-3 render for {site_name} exited without a result "
                f"(exitcode={proc.exitcode})") from None
        if status == "ok":
            return Path(output_path).read_text(encoding="utf-8")
        if kind == "TimeoutError":
            raise TimeoutError(message)
        raise RuntimeError(f"{kind}: {message}")
    finally:
        result_queue.close()
        result_queue.join_thread()
        with contextlib.suppress(FileNotFoundError):
            Path(output_path).unlink()


_OWN_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def _render_own_browser(site: SiteConfig) -> str:
    """Tier 3, own_browser: LAUNCH a dedicated Chromium (not CDP) and return the
    rendered HTML. A launched browser clears Cloudflare's "Just a moment" JS
    challenge that a CDP-attached one never does. Its own throwaway profile, no
    shared browser lock (it doesn't touch the shared host)."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        b = await p.chromium.launch(
            headless=False,
            ignore_default_args=["--enable-automation"],
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        try:
            ctx = await b.new_context(user_agent=_OWN_UA, locale="nl-NL")
            pg = await ctx.new_page()
            await pg.goto(site.list_url, wait_until="domcontentloaded", timeout=45000)
            await pg.wait_for_timeout(SETTLE_MS)
            return await pg.content()
        finally:
            await b.close()


async def poll_once(client: httpx.AsyncClient, site: SiteConfig) -> list[RawListing]:
    """One raw poll of a site: fetch + parse. Raises Blocked on a block signal."""
    parse = site.parse or parse_jsonld
    if site.tier == 3 and site.own_browser:
        html = await _render_own_browser(site)
        listings = parse(html, site)
        return _annotate_detected(site, listings)
    if site.tier == 3:
        html = await _render_tier3(site)
        listings = parse(html, site)
        return _annotate_detected(site, listings)
    result = await fetch(client, site)
    payload = result.json if site.tier == 1 else result.text
    listings = parse(payload, site)
    return _annotate_detected(site, listings)


def _annotate_detected(site: SiteConfig, listings: list[RawListing]) -> list[RawListing]:
    for listing in listings:
        listing.source_name = listing.source_name or site.name
        listing.detected_by = listing.detected_by or site.name
    return listings


async def _apply_worker(queue: asyncio.Queue[RawListing], seen: SeenStore) -> None:
    """Drain qualifying listings and run the existing apply pipeline (serialized;
    apply() takes the exclusive browser lock internally)."""
    from ..apply import apply  # local import: heavy deps, and avoids import cycle

    while True:
        listing = await queue.get()
        t0 = datetime.now()
        try:
            # Mail-triggered applies take precedence over speculative poller
            # ones (see ..apply_priority): don't even start while one is busy.
            if priority_pending():
                _log("apply_deferred", url=listing.source_url,
                     reason="priority mail apply in progress")
                while priority_pending():  # noqa: ASYNC110 — the flag is a cross-process file (apply_priority), no in-process Event can signal it
                    await asyncio.sleep(2)
            _log("apply_start", url=listing.source_url, source=listing.source_name)
            result = await asyncio.to_thread(
                apply, listing.to_listing(), yield_to_priority=True)
            seconds = round((datetime.now() - t0).total_seconds(), 1)
            _log("apply_done", url=listing.source_url,
                 outcome=result.outcome, rc=result.rc, seconds=seconds)
            if result.outcome == "yielded":
                # Aborted mid-run to hand the browser to a mail apply — not an
                # attempt, not a verdict. Requeue untouched (claim stays held);
                # the next dequeue waits out the priority window above.
                _log("apply_requeued", url=listing.source_url,
                     reason="yielded to a priority mail apply")
                await queue.put(listing)
                continue
            if result.outcome == "no_credit":
                # The LLM API refused for lack of credit (HTTP 402): the agent
                # never really ran, so — like a browser-lock timeout — this is
                # NOT an attempt. Release the claim so a future poll retries
                # after a top-up, and alert loudly (rate-limited): without the
                # carve-out every listing dropping during a credit outage was
                # consumed forever as "error".
                _log("apply_no_credit", url=listing.source_url)
                seen.release(listing.source_url)
                await asyncio.to_thread(
                    send_alert_dedup, "no_credit",
                    "💸 Stekkies bot: OUT of DeepSeek credit — applies are stopping",
                    "An apply run was refused with HTTP 402 (insufficient "
                    "balance). Listings are NOT consumed and will be retried "
                    "on future polls, but nothing submits until you top up:\n"
                    "  https://platform.deepseek.com/top_up\n",
                )
                continue
            from ..self_improvement_agent import improve_after_apply
            await asyncio.to_thread(
                improve_after_apply,
                listing=listing.to_listing().to_json(),
                result=result,
                trigger="poller",
                extra={"detected_by": listing.detected_by or listing.source_name},
            )
            message = result.summary or f"Poller agent finished with outcome={result.outcome} (rc={result.rc})."
            if not result.terminal:
                message += " One attempt per listing — no automatic retry."
            rec = _summary(
                source_url=listing.source_url,
                source=listing.source_name,
                detected_by=listing.detected_by or listing.source_name,
                address=listing.address or listing.title,
                status=result.outcome,
                returncode=result.rc,
                seconds=seconds,
                detected_ts=listing.detected_ts,
                message=message,
            )
            _activity(
                f"trigger=poller status={rec.get('status')} source={rec.get('source') or 'unknown source'} "
                f"address={rec.get('address') or 'unknown address'} - {rec.get('message')}"
            )
            # Off the loop: the Gmail send is synchronous network I/O and a
            # blocked event loop times out every in-flight httpx poll at once.
            await asyncio.to_thread(send_status_email, rec)
            # One attempt per listing: the agent ran, so the listing is
            # consumed whatever the outcome (see the no-retries note above).
            seen.mark(listing.source_url, outcome=result.outcome,
                      source=listing.source_name, address=listing.address)
            _remember_processed(listing, result.outcome, resolved_url=result.resolved_url)
        except TimeoutError as e:
            # Could not get the shared browser at all (lock contention) — the
            # agent never ran, no LLM cost was spent, so this is NOT an
            # attempt: release the claim and let a future poll re-qualify it.
            _log("apply_lock_timeout", url=listing.source_url, error=str(e))
            seen.release(listing.source_url)
        except Exception as e:  # noqa: BLE001 - one bad apply must not kill the worker
            _log("apply_error", url=listing.source_url, error=f"{type(e).__name__}: {e}")
            from ..self_improvement_agent import improve_exception
            await asyncio.to_thread(
                improve_exception,
                listing=listing.to_listing().to_json(),
                error=e,
                trigger="poller",
                extra={"detected_by": listing.detected_by or listing.source_name},
            )
            rec = _summary(
                source_url=listing.source_url,
                source=listing.source_name,
                detected_by=listing.detected_by or listing.source_name,
                address=listing.address or listing.title,
                status="error",
                detected_ts=listing.detected_ts,
                message=f"{type(e).__name__}: {e}. One attempt per listing — no automatic retry.",
            )
            await asyncio.to_thread(send_status_email, rec)
            # The apply crashed mid-flight; the agent may have spent real turns
            # already. Same one-attempt rule as above.
            seen.mark(listing.source_url, outcome="error",
                      source=listing.source_name, address=listing.address)
            _remember_processed(listing, "error")
        finally:
            queue.task_done()


async def _watch_site(site: SiteConfig, client: httpx.AsyncClient,
                      queue: asyncio.Queue[RawListing], seen: SeenStore) -> None:
    """Poll one site forever on its cadence, with jitter and block backoff."""
    # Stagger startup: all ~20 watchers firing in the same second contends
    # tier-3 sites on the browser lock and spikes DNS/executor demand.
    await asyncio.sleep(random.uniform(0, min(30.0, float(site.cadence_s))))
    blocks = 0
    zero_streak = 0
    while True:
        try:
            listings = await poll_once(client, site)
            blocks = 0
        except Blocked as e:
            blocks += 1
            wait = _BACKOFF[min(blocks - 1, len(_BACKOFF) - 1)]
            _log("blocked", site=site.name, streak=blocks, backoff_s=wait, reason=str(e))
            if blocks in (1, 3):  # alert on first block and if it persists
                await asyncio.to_thread(
                    send_alert,
                    f"⚠️ Poller blocked: {site.name}",
                    f"{site.name} returned a block/challenge signal.\n{e}\n"
                    f"Backing off {wait}s (streak {blocks}).",
                )
            await asyncio.sleep(wait)
            continue
        except TimeoutError:
            # Tier-3 only: the shared browser is busy (an apply run holds the
            # lock). A skipped speculative poll is normal operation, not an
            # error — try again next cadence.
            _log("browser_busy", site=site.name)
            await asyncio.sleep(site.cadence_s)
            continue
        except Exception as e:  # noqa: BLE001 - parse/network hiccup: log, keep going
            _log("poll_error", site=site.name, error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(site.cadence_s)
            continue

        if listings:
            zero_streak = 0
        else:
            zero_streak += 1
            if zero_streak == ZERO_YIELD_ALERT_POLLS:
                # A broken parser and "no listings right now" are
                # indistinguishable per poll; this streak is the only thing
                # that separates them (mijndak polled total=0 for days unnoticed
                # before it existed). Close the loop: hand the saved sample to
                # the self-improvement agent, which diagnoses and patches the
                # broken parser end-to-end (diagnose→fix registry/parsers→
                # verify→deploy), instead of emailing a human to do it. Runs off
                # the event loop (heavy: Claude SDK + git worktree) and is
                # deduped per site by the incident store. Only if
                # self-improvement is entirely disabled do we fall back to the
                # old alert email so a broken parser is never fully silent.
                diag_path = await _write_zero_yield_diagnostic(client, site)
                from ..self_improvement_agent import improve_poller_zero_yield
                rr = await asyncio.to_thread(
                    improve_poller_zero_yield,
                    site_name=site.name, list_url=site.list_url, tier=site.tier,
                    sample_path=diag_path, streak=zero_streak,
                )
                if rr is None:
                    await asyncio.to_thread(
                        send_alert_dedup, f"zero_yield:{site.name}",
                        f"🕳 Poller: {site.name} has yielded 0 listings for "
                        f"{ZERO_YIELD_ALERT_POLLS} consecutive polls",
                        f"{site.name} keeps answering with zero parsed listings "
                        f"({site.list_url}). Its parser may be silently broken — "
                        f"verify with: just poll-once {site.name}"
                        + (f"\nSaved sample: {diag_path}" if diag_path else ""),
                        min_interval_s=86400,
                    )

        new = [l for l in listings if seen.is_new(l.source_url)]
        _log("polled", site=site.name, total=len(listings), new=len(new))
        for l in new:
            await _consider(l, queue, seen)

        await asyncio.sleep(site.cadence_s + random.uniform(*site.jitter_s))


async def _enrich(l: RawListing) -> None:
    """Fill missing price/surface/description from the listing's own detail
    page (one cheap GET, fail-open). Anchor-parser sites yield URL-only
    listings, so without this the filter/judge fly blind on them and the
    description-based eligibility veto never sees its text. Tier-3 pages used
    to be skipped because many block plain httpx, but public tier-3 detail
    pages often still serve enough HTML for price/description. The fetch is
    cheap and fail-open, so try it before dropping a fresh listing as
    price-unknown."""
    cfg = by_name(l.detected_by or l.source_name)
    if cfg is not None and cfg.tier == 3 and cfg.needs_login:
        return
    if l.price is not None and l.surface is not None and l.description:
        return
    ctx = await asyncio.to_thread(fetch_context, l.source_url)
    if ctx is None:
        return
    l.price = l.price if l.price is not None else ctx.price
    l.surface = l.surface if l.surface is not None else ctx.surface
    l.description = l.description or ctx.description
    l.title = l.title or ctx.title
    l.city = l.city or ctx.city
    l.address = l.address or ctx.address


async def _consider(l: RawListing, queue: asyncio.Queue[RawListing],
                    seen: SeenStore) -> None:
    """Run deterministic filter -> LLM judgment; enqueue if it qualifies."""
    await _enrich(l)
    ok, reason = filters.passes(l)
    if not ok:
        _log("filtered_out", url=l.source_url, reason=reason)
        seen.mark(l.source_url, filtered=reason)  # won't re-evaluate a hard no
        return
    ok, reason = await judge.judge(l)
    if not ok:
        _log("judged_out", url=l.source_url, reason=reason)
        seen.mark(l.source_url, judged=reason)
        return
    l.detected_ts = l.detected_ts or eventlog.utc_now_iso()
    if not seen.reserve(l.source_url, source=l.source_name,
                        address=l.address or l.title):
        _log("duplicate_skipped", url=l.source_url, reason="already reserved")
        return
    _log("qualified", url=l.source_url, source=l.source_name,
         address=l.address, judge=reason)
    await queue.put(l)


async def run() -> None:
    sites = enabled_sites()
    seen = SeenStore()
    queue: asyncio.Queue[RawListing] = asyncio.Queue()
    # See EXECUTOR_THREADS: asyncio.to_thread AND the loop's own DNS lookups
    # share the default executor; it must be big enough that lock-waiting
    # tier-3 renders + a long apply can never starve getaddrinfo.
    asyncio.get_running_loop().set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=EXECUTOR_THREADS, thread_name_prefix="poll"))
    _log("watcher_started", sites=len(sites), executor_threads=EXECUTOR_THREADS)
    async with httpx.AsyncClient() as client:
        worker = asyncio.create_task(_apply_worker(queue, seen))
        watchers = [asyncio.create_task(_watch_site(s, client, queue, seen))
                    for s in sites]
        await asyncio.gather(worker, *watchers)


async def _once(name: str) -> None:
    """Diagnostic: one poll of one site, print candidates, do NOT apply."""
    site = by_name(name)
    if site is None:
        print(f"unknown site: {name}")
        return
    async with httpx.AsyncClient() as client:
        try:
            listings = await poll_once(client, site)
        except Blocked as e:
            print(f"BLOCKED: {e}")
            return
    print(f"# {len(listings)} listing(s) from {site.name} (tier {site.tier})")
    for l in listings:
        ok, reason = filters.passes(l)
        print(f"  [{'PASS' if ok else 'veto'}] {l.source_url}  "
              f"€{l.price} {l.surface}m² {l.city!r} — {reason}")


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        asyncio.run(_once(sys.argv[2]))
        return 0
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\n[poll] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
