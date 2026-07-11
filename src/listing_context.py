"""Pre-fetch a listing's own detail page for description/price/surface.

One cheap httpx GET per listing, used by ``apply.build_prompt``: the agent
used to spend its first turns reading the description in-browser (and
sometimes never found the requirements text at all). Pre-fetching it puts the
eligibility material, and any "apply via X" pointer the landlord wrote, in the
prompt up front.

Strictly fail-open: any error returns None and the caller proceeds exactly as
before. Tier-3 (Cloudflare/DataDome) pages simply fail the fetch — that's
fine, they already publish structured data on their list pages.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
}

_PRICE_RE = re.compile(r"(\d[\d.,\s]*)")


def _num(value) -> float | None:
    """Best-effort euro/number parse from '€ 1.750,00' / '1750' / 1750."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = _PRICE_RE.search(str(value))
    if not m:
        return None
    raw = m.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        # collapse a trailing ".00" cents artefact from the comma replace
        return float(raw)
    except ValueError:
        return None


class _LDExtractor(HTMLParser):
    """Pull the text of every <script type="application/ld+json"> block."""

    def __init__(self) -> None:
        super().__init__()
        self._in = False
        self.blocks: list[str] = []
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "script" and dict(attrs).get("type") == "application/ld+json":
            self._in = True
            self._buf = []

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._in:
            self._in = False
            self.blocks.append("".join(self._buf))


@dataclass
class _JsonLdListing:
    title: str = ""
    city: str = ""
    address: str = ""
    price: float | None = None
    surface: float | None = None
    description: str = ""


def _walk_ld(node, out: list[_JsonLdListing]) -> None:
    """Recurse JSON-LD, emitting a _JsonLdListing per housing/offer-ish node."""
    if isinstance(node, list):
        for n in node:
            _walk_ld(n, out)
        return
    if not isinstance(node, dict):
        return

    # @graph / itemListElement wrappers
    for key in ("@graph", "itemListElement", "item", "mainEntity"):
        if key in node:
            _walk_ld(node[key], out)

    types = node.get("@type", "")
    types = types if isinstance(types, list) else [types]
    types_l = {str(t).lower() for t in types}
    housing = {"residence", "apartment", "house", "singlefamilyresidence",
               "product", "offer", "realestatelisting", "accommodation"}
    if not (types_l & housing):
        return

    # A node with no url/@id is not a real listing entry (breadcrumb/nav
    # nodes on the same page share these @type values).
    url = node.get("url") or node.get("@id") or ""
    if isinstance(url, dict):
        url = url.get("@id") or url.get("url") or ""
    if not url:
        return

    offers = node.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    price = _num(offers.get("price") if isinstance(offers, dict) else None) \
        or _num(node.get("price"))

    addr = node.get("address") or {}
    city = ""
    address_str = ""
    if isinstance(addr, dict):
        city = addr.get("addressLocality") or ""
        address_str = " ".join(
            str(addr.get(k, "")) for k in ("streetAddress", "postalCode", "addressLocality")
        ).strip()
    elif isinstance(addr, str):
        address_str = addr

    surface = None
    fs = node.get("floorSize")
    if isinstance(fs, dict):
        surface = _num(fs.get("value"))

    out.append(_JsonLdListing(
        title=str(node.get("name", "")),
        price=price,
        city=str(city),
        address=address_str or str(node.get("name", "")),
        surface=surface,
        # Detail pages publish the full body text here (verified: huurportaal
        # RealEstateListing nodes carry ~2k chars) — feeds the eligibility
        # veto/prompt.
        description=str(node.get("description", ""))[:6000],
    ))


def parse_jsonld(payload: str) -> list[_JsonLdListing]:
    ex = _LDExtractor()
    try:
        ex.feed(payload)
    except Exception:  # noqa: BLE001 - malformed HTML shouldn't crash the fetch
        pass
    out: list[_JsonLdListing] = []
    for block in ex.blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        _walk_ld(data, out)
    return out


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
        listings = parse_jsonld(resp.text)
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
