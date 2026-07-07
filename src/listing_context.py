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

import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .poller.fetch import DEFAULT_HEADERS
from .poller.models import SiteConfig
from .poller.parsers import _num, parse_jsonld

# HTML fallback for anchor sites whose detail pages carry NO schema.org housing
# JSON-LD (verified 07-07-2026: ikwilhuren.nu detail pages have only
# Organization/breadcrumb LD, but show "€ 1.355,- /mnd" in plain HTML). Without
# this, 158 such listings were the single largest poller drop ("price unknown").
#
# A page has several € amounts (rent, parking, service costs, deposit), so we
# only trust € values with a PER-MONTH marker right after them and take the
# largest in a sane rent band — rent is ~always the biggest recurring monthly
# figure, and a one-time deposit ("borg") carries no /mnd marker. Deliberately
# conservative: a wrong guess that inflated the price would drop a valid
# listing, the exact false-negative we are trying to eliminate.
_MONTHLY_PRICE_RE = re.compile(
    r"€\s*([\d.,]+)\s*(?:,-\s*)?(?:[^0-9€]{0,50})"
    r"(?:/\s*mnd|/\s*maand|p\s*/?\s*m\b|per\s+maand|p\.m\.)",
    re.IGNORECASE,
)
_RENT_BAND = (300.0, 8000.0)


def _html_price(html: str) -> float | None:
    candidates = []
    for m in _MONTHLY_PRICE_RE.finditer(html):
        val = _num(m.group(1))
        if val is not None and _RENT_BAND[0] <= val <= _RENT_BAND[1]:
            candidates.append(val)
    return max(candidates) if candidates else None


# NB: surface is deliberately NOT extracted from raw HTML. A detail page shows
# several "N m2" figures (living area, storage/berging, balcony, plot) and the
# living area is not reliably first or largest; a wrong small value would only
# ever FALSELY DROP a valid listing (the surface filter only rejects, never
# rescues). Leaving surface None is strictly safer — an unknown surface passes
# the filter and the apply agent reads the real area on the page anyway.

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

    # Prefer the JSON-LD node with a real description (a detail page can also
    # embed Organization/breadcrumb nodes); anchor sites yield none at all.
    best = max(listings, key=lambda l: len(l.description)) if listings else None
    ctx = ListingContext(
        description=(best.description.strip() if best else ""),
        title=(best.title if best else ""),
        city=(best.city if best else ""),
        address=(best.address if best else ""),
        price=(best.price if best else None),
        surface=(best.surface if best else None),
    )
    # HTML fallback when JSON-LD gave no price (the anchor-site case). Surface
    # is intentionally left to JSON-LD only (see _html_price note above).
    if ctx.price is None:
        ctx.price = _html_price(resp.text)
    if not (ctx.description or ctx.price or ctx.surface):
        return None
    return ctx
