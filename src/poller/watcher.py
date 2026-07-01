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
import json
import random
import sys
from datetime import datetime

import httpx

from ..config import LOG_DIR
from ..notify import send_alert
from . import filters, judge
from .browser_lock import browser_lock
from .dedup import SeenStore
from .fetch import Blocked, fetch
from .models import RawListing, SiteConfig
from .parsers import parse_jsonld
from .registry import by_name, enabled_sites

POLL_LOG = LOG_DIR / "poller.jsonl"

# Backoff schedule (seconds) applied on consecutive blocks, capped.
_BACKOFF = [60, 300, 900, 1800, 3600]


def _log(event: str, **kw) -> None:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
    POLL_LOG.parent.mkdir(parents=True, exist_ok=True)
    with POLL_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[poll] {event}: " + " ".join(f"{k}={v}" for k, v in kw.items()))


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
                    await pg.goto(site.list_url, wait_until="networkidle")
                    await pg.wait_for_timeout(1500)
                    return await pg.content()
                finally:
                    await pg.close()
                    await b.close()
        return _a.run(_run())

    def _locked() -> str:
        with browser_lock():
            return _get_html()

    return await asyncio.to_thread(_locked)


async def poll_once(client: httpx.AsyncClient, site: SiteConfig) -> list[RawListing]:
    """One raw poll of a site: fetch + parse. Raises Blocked on a block signal."""
    parse = site.parse or parse_jsonld
    if site.tier == 3:
        html = await _render_tier3(site)
        return parse(html, site)
    result = await fetch(client, site)
    payload = result.json if site.tier == 1 else result.text
    return parse(payload, site)


async def _apply_worker(queue: "asyncio.Queue[RawListing]", seen: SeenStore) -> None:
    """Drain qualifying listings and run the existing apply pipeline (serialized;
    apply() takes the exclusive browser lock internally)."""
    from ..apply import apply  # local import: heavy deps, and avoids import cycle

    while True:
        listing = await queue.get()
        try:
            _log("apply_start", url=listing.source_url, source=listing.source_name)
            result = await asyncio.to_thread(apply, listing.to_listing())
            _log("apply_done", url=listing.source_url,
                 outcome=result.outcome, rc=result.rc)
            # Mark seen only on a terminal outcome, mirroring the orchestrator:
            # transient failures stay retryable on the next poll.
            if getattr(result, "terminal", True):
                seen.mark(listing.source_url, outcome=result.outcome,
                          source=listing.source_name, address=listing.address)
        except Exception as e:  # noqa: BLE001 - one bad apply must not kill the worker
            _log("apply_error", url=listing.source_url, error=f"{type(e).__name__}: {e}")
        finally:
            queue.task_done()


async def _watch_site(site: SiteConfig, client: httpx.AsyncClient,
                      queue: "asyncio.Queue[RawListing]", seen: SeenStore) -> None:
    """Poll one site forever on its cadence, with jitter and block backoff."""
    blocks = 0
    while True:
        try:
            listings = await poll_once(client, site)
            blocks = 0
        except Blocked as e:
            blocks += 1
            wait = _BACKOFF[min(blocks - 1, len(_BACKOFF) - 1)]
            _log("blocked", site=site.name, streak=blocks, backoff_s=wait, reason=str(e))
            if blocks in (1, 3):  # alert on first block and if it persists
                send_alert(
                    f"⚠️ Poller blocked: {site.name}",
                    f"{site.name} returned a block/challenge signal.\n{e}\n"
                    f"Backing off {wait}s (streak {blocks}).",
                )
            await asyncio.sleep(wait)
            continue
        except Exception as e:  # noqa: BLE001 - parse/network hiccup: log, keep going
            _log("poll_error", site=site.name, error=f"{type(e).__name__}: {e}")
            await asyncio.sleep(site.cadence_s)
            continue

        new = [l for l in listings if seen.is_new(l.source_url)]
        _log("polled", site=site.name, total=len(listings), new=len(new))
        for l in new:
            await _consider(l, queue, seen)

        await asyncio.sleep(site.cadence_s + random.uniform(*site.jitter_s))


async def _consider(l: RawListing, queue: "asyncio.Queue[RawListing]",
                    seen: SeenStore) -> None:
    """Run deterministic filter -> LLM judgment; enqueue if it qualifies."""
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
    _log("qualified", url=l.source_url, source=l.source_name,
         address=l.address, judge=reason)
    await queue.put(l)


async def run() -> None:
    sites = enabled_sites()
    seen = SeenStore()
    queue: "asyncio.Queue[RawListing]" = asyncio.Queue()
    _log("watcher_started", sites=len(sites))
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
