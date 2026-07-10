"""httpx fetch + block/challenge detection for tiers 1 and 2.

Distinguishes "no new listings" from "we've been blocked":
  - not HTTP 200 (403/429/503/challenge redirect) -> Blocked
  - 200 but the body is a Cloudflare/CAPTCHA interstitial -> Blocked
  - 200 but the expected shape is missing (JSON decode fails for tier 1) -> Blocked

The caller (watcher) reacts to Blocked with exponential backoff + notify, and
never treats a Blocked poll as "empty".
"""
from __future__ import annotations

import httpx

from .models import SiteConfig

# A realistic Chrome header set; per-site headers in SiteConfig override/extend
# this. A full set (not just UA) gets past naive UA-only 403 filters. It does NOT
# beat TLS-fingerprint/Cloudflare blocks (pararius/huurwoningen) — those are
# tier-3 (real browser) in the registry.
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "nl-NL,nl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Sec-Ch-Ua": '"Chromium";v="126", "Google Chrome";v="126", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# Strong interstitial markers: their presence means an anti-bot wall regardless
# of page size (they don't appear on real listing pages).
_STRONG_MARKERS = (
    "cf-browser-verification",
    "challenge-platform",
    "just a moment",
    "attention required",
    "datadome",
    "px-captcha",
    "/cdn-cgi/challenge",
)
# Weak markers: real pages legitimately embed these (e.g. a reCAPTCHA on a
# contact form), so they only count as a block on a TINY body — actual
# interstitials are a few KB at most, real listing pages are tens of KB.
_WEAK_MARKERS = ("captcha", "access denied", "are you a human", "verifying you are human")
_SMALL_BODY = 2500


class Blocked(Exception):
    """Raised when a poll response indicates we were blocked/challenged."""


class FetchResult:
    __slots__ = ("status", "text", "json")

    def __init__(self, status: int, text: str, json_body):
        self.status = status
        self.text = text
        self.json = json_body


def _looks_challenged(body: str) -> bool:
    low = body[:4000].lower()
    if any(m in low for m in _STRONG_MARKERS):
        return True
    return len(body) < _SMALL_BODY and any(m in low for m in _WEAK_MARKERS)


async def fetch(client: httpx.AsyncClient, site: SiteConfig) -> FetchResult:
    """Fetch a site's list payload. Raises ``Blocked`` on a block signal.

    For tier 1, a JSON decode failure is treated as a block (schema mismatch /
    interstitial), not an empty result.
    """
    url = site.target_url
    headers = {**DEFAULT_HEADERS, **site.headers}
    resp = await client.request(
        site.method, url, params=site.params or None,
        headers=headers, follow_redirects=True, timeout=20.0,
    )
    body = resp.text

    if resp.status_code != 200:
        raise Blocked(f"HTTP {resp.status_code} from {url}")
    if _looks_challenged(body):
        raise Blocked(f"challenge/interstitial body from {url}")

    json_body = None
    if site.tier == 1:
        try:
            json_body = resp.json()
        except Exception as e:  # noqa: BLE001 - any decode failure = unusable
            raise Blocked(f"tier-1 JSON decode failed for {url}: {e}") from e

    return FetchResult(resp.status_code, body, json_body)
