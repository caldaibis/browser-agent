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
import json
import os
import random
import sys
from datetime import datetime

import httpx

from ..apply_priority import priority_pending
from ..config import LOG_DIR
from ..listing_context import fetch_context
from ..notify import send_alert, send_alert_dedup, send_status_email
from . import filters, judge
from .browser_lock import browser_lock
from .dedup import PROCESSED_FILE, SeenStore
from .fetch import Blocked, fetch
from .models import RawListing, SiteConfig
from .parsers import parse_jsonld
from .registry import by_name, enabled_sites

POLL_LOG = LOG_DIR / "poller.jsonl"
MAIL_SUMMARY_LOG = LOG_DIR / "mail_summary.jsonl"
ACTIVITY_LOG = LOG_DIR / "activity.log"

# Tier-3 render settle time (ms) after DOM load, for the listing JS to populate.
SETTLE_MS = int(os.environ.get("POLL_TIER3_SETTLE_MS", "5500"))

# Size of the event loop's default thread executor. The default
# (min(32, cpus+4) — 8 threads on a 4-vCPU VPS) is far too small here:
# every tier-3 render and every apply parks a thread in asyncio.to_thread
# waiting on the browser flock, and asyncio resolves DNS (loop.getaddrinfo)
# on that SAME executor. With ~13 tier-3 watchers a full executor starves
# DNS, so every pending httpx connect times out AT ONCE — observed as 10k+
# simultaneous ConnectTimeout poll_errors per day across all tier-2 sites
# (07-07-2026), i.e. ~80% of tier-2 polls silently lost.
EXECUTOR_THREADS = int(os.environ.get("POLL_EXECUTOR_THREADS", "64"))

# A speculative tier-3 poll must not queue behind a long apply for the shared
# browser: skip the poll and try again next cadence (also frees its executor
# thread quickly — see EXECUTOR_THREADS).
TIER3_LOCK_TIMEOUT = float(os.environ.get("POLL_TIER3_LOCK_TIMEOUT", "120"))

# Consecutive zero-listing polls before suspecting a silently-broken parser:
# a parser that matches nothing looks exactly like "no new listings", forever.
ZERO_YIELD_ALERT_POLLS = int(os.environ.get("POLL_ZERO_YIELD_ALERT_POLLS", "120"))

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
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
    POLL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with POLL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[poll] {event}: " + " ".join(f"{k}={v}" for k, v in kw.items()))


def _summary(**kw) -> dict:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "trigger": "poller", **kw}
    MAIL_SUMMARY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with MAIL_SUMMARY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _activity(message: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {message}"
    with ACTIVITY_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _remember_processed(listing: RawListing, outcome: str, resolved_url: str = "") -> None:
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": datetime.now().isoformat(timespec="seconds"),
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
    with PROCESSED_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


async def _render_tier3(site: SiteConfig) -> str:
    """Tier 3: open a real tab over CDP (under the browser lock) and return its
    HTML. Used only for sites that defeat httpx (JS-gated / login-walled list)."""
    from playwright.async_api import async_playwright
    from ..config import CDP_URL

    def _get_html() -> str:
        import asyncio as _a

        async def _run() -> str:
            async with async_playwright() as p:
                b = await p.chromium.connect_over_cdp(CDP_URL)
                ctx = b.contexts[0] if b.contexts else await b.new_context()
                pg = await ctx.new_page()
                try:
                    # domcontentloaded + a fixed settle beats "networkidle":
                    # many listing SPAs (funda) hold connections open and never
                    # go idle, which would time the goto out.
                    await pg.goto(site.list_url, wait_until="domcontentloaded",
                                  timeout=45000)
                    await pg.wait_for_timeout(SETTLE_MS)
                    return await pg.content()
                finally:
                    await pg.close()
                    await b.close()
        return _a.run(_run())

    def _locked() -> str:
        with browser_lock(timeout=TIER3_LOCK_TIMEOUT, holder=f"tier3:{site.name}"):
            return _get_html()

    return await asyncio.to_thread(_locked)


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


async def _apply_worker(queue: "asyncio.Queue[RawListing]", seen: SeenStore) -> None:
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
                while priority_pending():
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
                listing=listing.to_listing(),
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
                listing=listing.to_listing(),
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
                      queue: "asyncio.Queue[RawListing]", seen: SeenStore) -> None:
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
                # indistinguishable per poll; this streak-alert is the only
                # thing that separates them. (mijndak polled total=0 for days
                # unnoticed before this existed.)
                await asyncio.to_thread(
                    send_alert_dedup, f"zero_yield:{site.name}",
                    f"🕳 Poller: {site.name} has yielded 0 listings for "
                    f"{ZERO_YIELD_ALERT_POLLS} consecutive polls",
                    f"{site.name} keeps answering with zero parsed listings "
                    f"({site.list_url}). Its parser may be silently broken — "
                    f"verify with: just poll-once {site.name}",
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
    description-based eligibility veto never sees its text. Tier-3 sites are
    skipped: their detail pages block plain httpx anyway."""
    cfg = by_name(l.detected_by or l.source_name)
    if cfg is not None and cfg.tier == 3:
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


async def _consider(l: RawListing, queue: "asyncio.Queue[RawListing]",
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
    l.detected_ts = l.detected_ts or datetime.now().isoformat(timespec="seconds")
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
    queue: "asyncio.Queue[RawListing]" = asyncio.Queue()
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
