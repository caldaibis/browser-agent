"""Pre-fetch a listing's own detail page for description/price/surface.

One cheap httpx GET per listing, used in two places:

  - ``poller.watcher._consider``: anchor-parser sites yield URL-only listings
    (no price/surface/description), so the deterministic filter and the judge
    were flying blind on them; a detail-page fetch fills the gaps before any
    LLM cost is spent.
  - ``apply.build_prompt``: the agent used to spend its first turns reading
    the description in-browser (and sometimes never found the requirements
    text at all). Pre-fetching it puts the eligibility material, and any
    "apply via X" pointer the landlord wrote, in the prompt up front.

Strictly fail-open: any error returns None and the caller proceeds exactly as
before. Tier-3 (Cloudflare/DataDome) pages simply fail the fetch — that's
fine, they already publish structured data on their list pages.
"""
from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .poller.fetch import DEFAULT_HEADERS
from .poller.models import SiteConfig
from .poller.parsers import parse_jsonld

# Sites that are shop windows: the page describes the listing, but the real
# application usually happens on the landlord/agency's own site behind the
# page's apply/contact action. The apply prompt warns the agent so it doesn't
# wander off searching the agency site by hand (a run burned 55 of 60 turns
# doing exactly that on 03-07-2026).
AGGREGATOR_DOMAINS = {"huurportaal.nl", "huurwoningen.nl", "pararius.nl"}


@dataclass
class ListingContext:
    description: str = ""
    title: str = ""
    city: str = ""
    address: str = ""
    price: float | None = None
    surface: float | None = None


def _domain(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_aggregator(url: str) -> bool:
    return _domain(url) in AGGREGATOR_DOMAINS


def fetch_context(url: str, timeout: float = 8.0) -> ListingContext | None:
    """GET the listing page and pull JSON-LD facts out of it. None on any
    failure or when the page yields nothing usable."""
    try:
        resp = httpx.get(url, headers=DEFAULT_HEADERS, timeout=timeout,
                         follow_redirects=True)
        if resp.status_code != 200:
            return None
        # Reuse the generic JSON-LD walker against the DETAIL page; the fake
        # SiteConfig only anchors relative-URL resolution.
        listings = parse_jsonld(resp.text, SiteConfig(name=_domain(url), list_url=url))
    except Exception:  # noqa: BLE001 - strictly fail-open, see module docstring
        return None
    if not listings:
        return None
    # Prefer the node with a real description (a detail page can also embed
    # e.g. Organization/breadcrumb nodes); fall back to the first.
    best = max(listings, key=lambda l: len(l.description))
    ctx = ListingContext(
        description=best.description.strip(),
        title=best.title,
        city=best.city,
        address=best.address,
        price=best.price,
        surface=best.surface,
    )
    if not (ctx.description or ctx.price or ctx.surface):
        return None
    return ctx
