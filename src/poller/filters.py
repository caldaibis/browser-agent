"""Deterministic pre-filter on structured RawListing fields.

Cheap, runs before any LLM judgment. Rent is a hard user cap, so poller listings
must have a known parsed price by default; URL-only parser output is not enough
to submit autonomously.

The distance-to-center and roommate judgments are NOT here — they need semantic
reading of the address/description and live in ``judge.py``.
"""
from __future__ import annotations

import os

from ..rent_policy import MAX_RENT
from .models import RawListing

MIN_RENT = float(os.environ.get("POLL_MIN_PRICE", "800"))
MIN_SURFACE = float(os.environ.get("POLL_MIN_SURFACE", "30"))
REQUIRE_KNOWN_PRICE = os.environ.get("POLL_REQUIRE_KNOWN_PRICE", "1") != "0"
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
    if listing.price is None and REQUIRE_KNOWN_PRICE:
        return False, f"price unknown; rent range is €{MIN_RENT:.0f}-€{MAX_RENT:.0f}"
    if listing.price is not None and listing.price < MIN_RENT:
        return False, f"price €{listing.price:.0f} < €{MIN_RENT:.0f}"
    if listing.price is not None and listing.price > MAX_RENT:
        return False, f"price €{listing.price:.0f} > €{MAX_RENT:.0f}"

    # City often lives only in the URL path (e.g. huurwoningen's JSON-LD address
    # is just the street), so include source_url in the haystack. This both
    # rescues real listings whose parsed address lacks a locality AND cheaply
    # vetoes other-city listings (e.g. an ikwilhuren /object/delft-... URL)
    # before they cost an LLM-judge call.
    haystack = f"{listing.city} {listing.address} {listing.source_url}".lower()
    if CITIES and haystack.strip() and not any(c in haystack for c in CITIES):
        return False, f"city not in {CITIES}: {(listing.city + ' ' + listing.address).strip()!r}"

    if listing.surface is not None and listing.surface < MIN_SURFACE:
        return False, f"surface {listing.surface:.0f}m² < {MIN_SURFACE:.0f}m²"

    label = f"{listing.listing_type} {listing.title}".lower()
    if any(m in label for m in _ROOM_MARKERS):
        return False, f"looks like a room/share: {label.strip()!r}"

    return True, "ok"
