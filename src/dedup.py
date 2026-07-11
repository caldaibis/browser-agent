"""Canonical-URL dedup for cross-site listings.

The same flat surfaces on pararius + huurwoningen + a makelaar site; keying on
the tracking-stripped source URL collapses them to one apply. Also used to
recognize a listing already handled via a different mail trigger (Stekkies vs
Huurwoningen alert) so it is never re-applied.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse

# Query params that never identify a listing — strip them before keying.
_TRACKING_PREFIXES = ("utm_", "gclid", "fbclid", "mc_")
_TRACKING_EXACT = {"ref", "referrer", "source", "src", "session", "sid", "cid"}

# Site-specific canonicalization: some sites expose the SAME listing under
# several unrelated URL shapes, which path-based keying can never connect.
# Verified on huurwoningen.nl (Kaatstraat, 02-07-2026): the alert-mail
# deep-link is /frontend/listing/<full-uuid>/?alt=... while the site page is
# /huren/<city>/<uuid-first-8-hex>/<street-slug>/. The shared listing id is
# the first UUID group. Two mails for the same flat therefore produced two
# different keys, the pre-flight duplicate check matched neither, and a full
# agent run was spent just to hit the mid-run duplicate guard. Both shapes
# collapse to a synthetic key: https://huurwoningen.nl/listing/<hex8>.
# Backward compatible: every reader re-canonicalizes stored keys/urls at load
# time, so keys written before this rule map to the new form too.
_HUURWONINGEN_LISTING_RES = (
    re.compile(r"^/frontend/listing/([0-9a-fA-F]{8})[0-9a-fA-F-]*/?"),
    re.compile(r"^/huren/[^/]+/([0-9a-fA-F]{8})(?:/|$)"),
)


def _site_listing_key(host: str, path: str) -> str | None:
    if host == "huurwoningen.nl":
        for rx in _HUURWONINGEN_LISTING_RES:
            m = rx.match(path)
            if m:
                return f"https://huurwoningen.nl/listing/{m.group(1).lower()}"
    return None


def canonical_url(url: str) -> str:
    """Normalize to scheme+host+path, lowercased host, no tracking query, no
    trailing slash. Query params that look like real identifiers are kept and
    sorted for stability. Sites listed in _site_listing_key collapse further,
    to a per-listing-id key that is stable across that site's URL shapes."""
    p = urlparse(url.strip())
    host = (p.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    scheme = p.scheme or "https"
    path = p.path.rstrip("/") or "/"

    site_key = _site_listing_key(host, p.path)
    if site_key:
        return site_key

    kept = []
    for pair in p.query.split("&"):
        if not pair or "=" not in pair:
            continue
        k = pair.split("=", 1)[0].lower()
        if k in _TRACKING_EXACT or any(k.startswith(pref) for pref in _TRACKING_PREFIXES):
            continue
        kept.append(pair)
    query = "&".join(sorted(kept))

    netloc = host + (f":{p.port}" if p.port else "")
    return urlunparse((scheme, netloc, path, "", query, ""))


def known_processed_urls() -> set[str]:
    """Canonical URLs already recorded as done, from the SQLite store —
    including any resolved_url an apply run discovered mid-flight (see
    browser_agent.py's mid-run duplicate check).

    This is the read side of a real gap, not a hypothetical: the Stekkies
    mail path and the Huurwoningen mail alert can each record the SAME
    real-world listing under a different external source URL (e.g. after
    in-page redirect dialogs on an aggregator page). Reaching the final URL
    from an aggregator page requires actually clicking through those
    redirects (not something a plain fetch could resolve up front), so this
    can't be checked before opening the browser. browser_agent.py calls this
    once per turn to catch the moment the browser reaches an already-known
    destination, not just before starting."""
    try:
        from . import store  # late import: store imports models imports this module

        return {canonical_url(k) for k in store.processed_keys()}
    except Exception:
        return set()
