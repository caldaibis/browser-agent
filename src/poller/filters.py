"""Deterministic pre-filter on structured RawListing fields.

Cheap, runs before any LLM judgment. Only vetoes on facts the site actually
published; anything unknown passes through to the LLM/apply stage (fail-open,
because in this market a missed listing is worse than a wasted look).

The distance-to-center and roommate judgments are NOT here — they need semantic
reading of the address/description and live in ``judge.py``.
"""
from __future__ import annotations

import os

from .models import RawListing

MAX_PRICE = float(os.environ.get("POLL_MAX_PRICE", "1750"))
MIN_SURFACE = float(os.environ.get("POLL_MIN_SURFACE", "30"))
# Cities we apply in. Matched case-insensitively as a substring of city/address.
CITIES = tuple(
    c.strip().lower()
    for c in os.environ.get("POLL_CITIES", "utrecht,amsterdam").split(",")
    if c.strip()
)

# Obvious room/share markers in the raw type/title. The nuanced judgment (a flat
# described as shared without the word "kamer") is left to the LLM.
_ROOM_MARKERS = ("kamer", "room", "studentenkamer", "shared", "gedeeld")


def passes(listing: RawListing) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means a hard, published fact vetoes it."""
    if listing.price is not None and listing.price > MAX_PRICE:
        return False, f"price €{listing.price:.0f} > €{MAX_PRICE:.0f}"

    haystack = f"{listing.city} {listing.address}".lower()
    if CITIES and haystack.strip() and not any(c in haystack for c in CITIES):
        return False, f"city not in {CITIES}: {haystack.strip()!r}"

    if listing.surface is not None and listing.surface < MIN_SURFACE:
        return False, f"surface {listing.surface:.0f}m² < {MIN_SURFACE:.0f}m²"

    label = f"{listing.listing_type} {listing.title}".lower()
    if any(m in label for m in _ROOM_MARKERS):
        return False, f"looks like a room/share: {label.strip()!r}"

    return True, "ok"
