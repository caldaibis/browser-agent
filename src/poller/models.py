"""Data models for the active listing poller.

`RawListing` is the site-agnostic listing shape every parser must produce.
`SiteConfig` describes how to watch one site (which tier, where, how to parse).

See docs/poller-plan.md for the design these types implement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..models import Listing


@dataclass
class RawListing:
    """One listing as seen by the watcher, before dedup/filter/judgment.

    Only ``source_url`` is required for the downstream apply pipeline; the rest
    is best-effort and used for deterministic filtering + LLM judgment.
    """
    source_url: str
    source_name: str = ""
    address: str = ""
    city: str = ""
    price: float | None = None      # euros/month, numeric when known
    surface: float | None = None    # m², numeric when known
    listing_type: str = ""             # e.g. "apartment", "room" — raw site label
    title: str = ""
    description: str = ""              # listing body text when the site publishes it
    detected_by: str = ""              # poller registry site that found it
    detected_ts: str = ""              # when the watcher first qualified it

    def to_listing(self) -> Listing:
        """The typed pipeline Listing handed to ``apply.apply()`` — it needs
        only source_url; the rest enriches the prompt/logs."""
        from ..models import Listing  # runtime late import: src.models imports poller.dedup

        return Listing(
            source_url=self.source_url,
            source_name=self.source_name or "poller",
            detected_by=self.detected_by,
            address=self.address or self.title or "?",
            price=f"€{self.price:.0f}" if self.price is not None else "?",
            description=self.description[:4000] if self.description else "",
        )


# A parser turns a fetched payload (JSON dict/list for tier 1, HTML str for
# tier 2/3) into RawListings. It must be pure and tolerant of schema drift
# (raise or return [] on unexpected shapes so block-detection can react).
Parser = Callable[[object, "SiteConfig"], "list[RawListing]"]


@dataclass
class SiteConfig:
    """How to watch one source site. Discovery (docs/poller-plan.md) fills this."""
    name: str                              # canonical host, e.g. "huurwoningen.nl"
    tier: int = 2                          # 1=JSON API, 2=filtered HTML, 3=rendered browser
    endpoint: str = ""                     # tier 1: JSON API URL
    list_url: str = ""                     # tier 2/3: filtered listing page URL
    method: str = "GET"
    params: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    parse: Parser | None = None         # payload -> list[RawListing]; None = generic
    needs_login: bool = False              # list requires an authed profile (tier 3)
    own_browser: bool = False              # tier 3: LAUNCH a dedicated browser
    #   instead of attaching to the shared host over CDP. Needed for sites whose
    #   anti-bot (Cloudflare "Just a moment") detects the CDP attachment — a
    #   freshly launched Chromium clears the JS challenge, a CDP-attached one
    #   never does. Uses its own throwaway profile (no shared logins), so it is
    #   for public listing pages that don't need our stored session.
    cadence_s: int = 60                    # base poll interval
    jitter_s: tuple[int, int] = (0, 30)    # random extra delay added per poll
    enabled: bool = True                   # discovery-incomplete sites start disabled

    @property
    def target_url(self) -> str:
        return self.endpoint if self.tier == 1 else self.list_url
