"""Generic, site-agnostic parsers used as tier-2 defaults.

Per-site discovery can replace these with precise parsers in the registry, but
these get a site watchable on day one without reverse-engineering:

  - ``parse_jsonld``: many Dutch rental sites embed schema.org listings as
    ``<script type="application/ld+json">`` in server-rendered HTML. This reads
    Residence/Apartment/Product/Offer nodes into RawListings.
  - ``parse_anchors``: last-ditch — collect listing-detail links whose path
    matches a per-site pattern, so at least new URLs get noticed.

Both are tolerant: unexpected shapes yield [] (the watcher's block-detector,
not the parser, decides whether an empty result means trouble).
"""
from __future__ import annotations

import html
import json
import re
from collections.abc import Callable
from html.parser import HTMLParser
from urllib.parse import urljoin

from .models import RawListing, SiteConfig

_PRICE_RE = re.compile(r"(\d[\d.,\s]*)")
_SURFACE_RE = re.compile(r"(\d+)\s*m")
_MONTHLY_PRICE_RE = re.compile(
    r"(?:€|eur)\s*([\d.,\s]+)\s*(?:,-\s*)?"
    r"(?:[^0-9€]{0,50})"
    r"(?:/\s*(?:mnd|maand)|p\s*/?\s*m\b|per\s+maand|pm\b|p\.m\.)",
    re.IGNORECASE,
)
_SURFACE_HTML_RE = re.compile(r"(\d{2,4})\s*m\s*(?:<sup>\s*2|²|2)?", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


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


def _clean_text(text: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _html_monthly_price(fragment: str) -> float | None:
    candidates = []
    for m in _MONTHLY_PRICE_RE.finditer(fragment or ""):
        val = _num(m.group(1))
        if val is not None and 300 <= val <= 8000:
            candidates.append(val)
    return max(candidates) if candidates else None


def _html_surface(fragment: str) -> float | None:
    text = fragment or ""
    for m in _SURFACE_HTML_RE.finditer(text):
        val = _num(m.group(1))
        if val is not None and 10 <= val <= 1000:
            return val
    return None


def _link_context(doc: str, start: int, end: int, radius: int = 1800) -> str:
    """Small surrounding HTML chunk for a listing link.

    Rendered tier-3 listing cards usually contain URL, price, m2, and address
    near each other. This is deliberately heuristic but local to the link so a
    random footer or unrelated listing price is unlikely to contaminate it.
    """
    left = max(0, start - radius)
    right = min(len(doc), end + radius)
    return doc[left:right]


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


def _walk_ld(node, base_url: str, out: list[RawListing]) -> None:
    """Recurse JSON-LD, emitting a RawListing per housing/offer-ish node."""
    if isinstance(node, list):
        for n in node:
            _walk_ld(n, base_url, out)
        return
    if not isinstance(node, dict):
        return

    # @graph / itemListElement wrappers
    for key in ("@graph", "itemListElement", "item", "mainEntity"):
        if key in node:
            _walk_ld(node[key], base_url, out)

    types = node.get("@type", "")
    types = types if isinstance(types, list) else [types]
    types_l = {str(t).lower() for t in types}
    housing = {"residence", "apartment", "house", "singlefamilyresidence",
               "product", "offer", "realestatelisting", "accommodation"}
    if not (types_l & housing):
        return

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

    out.append(RawListing(
        source_url=urljoin(base_url, str(url)),
        title=str(node.get("name", "")),
        price=price,
        city=str(city),
        address=address_str or str(node.get("name", "")),
        surface=surface,
        listing_type=" ".join(sorted(types_l)),
        # Detail pages publish the full body text here (verified: huurportaal
        # RealEstateListing nodes carry ~2k chars) — feeds the deterministic
        # eligibility veto + the judge + the apply prompt.
        description=str(node.get("description", ""))[:6000],
    ))


def parse_jsonld(payload: object, site: SiteConfig) -> list[RawListing]:
    html = payload if isinstance(payload, str) else ""
    ex = _LDExtractor()
    try:
        ex.feed(html)
    except Exception:  # noqa: BLE001 - malformed HTML shouldn't crash the poll
        pass
    out: list[RawListing] = []
    base = site.list_url or site.endpoint
    for block in ex.blocks:
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        _walk_ld(data, base, out)
    for l in out:
        l.source_name = l.source_name or site.name
    return out


def make_anchor_parser(
    path_pattern: str,
) -> Callable[[object, SiteConfig], list[RawListing]]:
    """Build a parser that scrapes listing-detail links matching a regex on the
    URL path. Best-effort tier-2 fallback: yields URLs only (no price/surface),
    which then rely on the LLM/apply stage."""
    href_re = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.I | re.S)
    path_re = re.compile(path_pattern)

    def parse(payload: object, site: SiteConfig) -> list[RawListing]:
        doc = payload if isinstance(payload, str) else ""
        base = site.list_url or site.endpoint
        seen: set[str] = set()
        out: list[RawListing] = []
        for m in href_re.finditer(doc):
            href, label_html = m.group(1), m.group(2)
            full = urljoin(base, html.unescape(href))
            if not path_re.search(full) or full in seen:
                continue
            seen.add(full)
            context = _link_context(doc, m.start(), m.end())
            title = _clean_text(label_html)[:240]
            context_text = _clean_text(context)
            out.append(RawListing(
                source_url=full,
                source_name=site.name,
                title=title,
                address=title,
                price=_html_monthly_price(context),
                surface=_html_surface(context),
                description=context_text[:1200],
            ))
        return out

    return parse
