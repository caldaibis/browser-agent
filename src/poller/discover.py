"""Discovery probe — run the per-site spike from docs/poller-plan.md at scale.

For each registered site: fetch its ``list_url`` with httpx and report
  - HTTP status / blocked?
  - how many listings the generic JSON-LD parser found
  - a suggested next step (tier-2 works / needs tier-1 API / needs tier-3 tab)

This turns the "structured discovery" checklist into a runnable report so you
can see at a glance which of the 26 sites already work with the generic parser
and which need hand discovery (API reverse-engineering or a rendered tab).

Run:  python -m src.poller.discover
      python -m src.poller.discover pararius.nl   # just one site
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from . import filters
from .fetch import Blocked, fetch
from .parsers import parse_jsonld
from .registry import REGISTRY, by_name


async def _probe(client: httpx.AsyncClient, site) -> str:
    off = "" if site.enabled else " (off)"
    # Tier-3 sites are httpx-blocked BY DESIGN; they run via the browser host.
    if site.tier == 3:
        return f"TIER3     {site.name:32}{off} rendered-tab (needs browser host){' login' if site.needs_login else ''}"
    try:
        result = await fetch(client, site)
    except Blocked as e:
        return f"BLOCKED   {site.name:32}{off} {e}"
    except Exception as e:  # noqa: BLE001
        return f"ERROR     {site.name:32}{off} {type(e).__name__}: {e}"

    payload = result.json if site.tier == 1 else result.text
    parser = site.parse or parse_jsonld
    try:
        listings = parser(payload, site)
    except Exception as e:  # noqa: BLE001
        return f"PARSEFAIL {site.name:32} {type(e).__name__}: {e}"

    passing = sum(1 for l in listings if filters.passes(l)[0])
    tier = f"tier{site.tier}"
    if listings:
        return (f"OK        {site.name:32}{off} {tier} works: "
                f"{len(listings)} listings ({passing} pass filter)")
    if site.tier == 1:
        return f"OK        {site.name:32}{off} tier1 API up: 0 available right now"
    return (f"EMPTY     {site.name:32}{off} no listings in HTML — "
            f"needs tier-1 API cURL (SPA)")


async def _run(names: list[str]) -> None:
    sites = [by_name(n) for n in names] if names else REGISTRY
    sites = [s for s in sites if s is not None]
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(_probe(client, s) for s in sites))
    for line in results:
        print(line)
    print(f"\n{len(sites)} site(s) probed.")


def main() -> int:
    asyncio.run(_run(sys.argv[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
