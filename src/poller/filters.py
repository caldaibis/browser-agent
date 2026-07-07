"""Deterministic pre-filter on structured RawListing fields.

Cheap, runs before any LLM judgment. Rent is a hard user cap, so poller listings
must have a known parsed price by default; URL-only parser output is not enough
to submit autonomously.

The distance-to-center and roommate judgments are NOT here — they need semantic
reading of the address/description and live in ``judge.py``.
"""
from __future__ import annotations

import os
import re

from ..rent_policy import MAX_RENT
from .models import RawListing

MIN_RENT = float(os.environ.get("POLL_MIN_PRICE", "800"))
MIN_SURFACE = float(os.environ.get("POLL_MIN_SURFACE", "30"))
REQUIRE_KNOWN_PRICE = os.environ.get("POLL_REQUIRE_KNOWN_PRICE", "1") != "0"
# Cities we apply in. Matched case-insensitively as a substring of city/address.
CITIES = tuple(
    c.strip().lower()
    for c in os.environ.get("POLL_CITIES", "utrecht").split(",")
    if c.strip()
)

# Obvious room/share markers in the raw type/title. The nuanced judgment (a flat
# described as shared without the word "kamer") is left to the LLM.
_ROOM_MARKERS = ("kamer", "room", "studentenkamer", "shared", "gedeeld")

# ---------------------------------------------------------------------------
# Hard published eligibility gates in the listing TEXT. Before this existed,
# full agent runs were spent opening the browser just to read "ALLEEN
# BESCHIKBAAR VOOR STUDENTEN" in the description (huurportaal, 02-07-2026 —
# twice in one day) — text that was available at poll time for free.
#
# Deliberately conservative: only phrasings that RESTRICT the listing to a
# group the applicant is not (a working professional, not a student, not a
# senior). "studenten en woningdelers behoren NIET tot onze doelgroep" is the
# opposite (students excluded — fine for us), so any sentence containing a
# negation word is skipped and left to the judge/agent.
_NEGATIONS = ("geen", "niet", "not", "uitgesloten", "behalve")
_STUDENT_ONLY_RES = (
    re.compile(r"\b(alleen|uitsluitend|enkel|only|exclusief)\b[^.!?\n|]*\bstudent"),
    re.compile(r"\bstudent(?:s|en)?\s+only\b"),
    re.compile(r"\bvoor\s+(?:een\s+)?student(?:en)?\s+(?:van|uit|gezocht)\b"),
    re.compile(r"\bstudentenwoning\b|\bstudentencomplex\b|\bstudentenhuis\b"),
)
_SENIOR_ONLY_RES = (
    re.compile(r"\b(alleen|uitsluitend|enkel|only|exclusief)\b[^.!?\n|]*\bsenior"),
    re.compile(r"\bseniorenwoning\b|\bseniorencomplex\b|\b55\s*\+\s*(?:woning|complex|appartement)"),
)
_SHORT_STAY_RES = (
    re.compile(r"\bshort[\s-]?stay\b"),
    re.compile(r"\b(?:maximaal|max\.?|voor maximaal)\s*(\d{1,2})\s*maand"),
    re.compile(r"\b(?:maximum|max\.?)\s*(?:of\s*)?(\d{1,2})\s*months?\b"),
)
# A max stay below this is pointless for someone seeking a long-term home.
MIN_STAY_MONTHS = int(os.environ.get("POLL_MIN_STAY_MONTHS", "6"))

_SENTENCE_SPLIT = re.compile(r"[.!?\n|]+")


def hard_exclusion(text: str) -> str | None:
    """Return a veto reason when the text states a hard gate the applicant
    can never pass, else None. Sentence-scoped so negated mentions
    ("geen studenten") never trigger it."""
    for sentence in _SENTENCE_SPLIT.split((text or "").lower()):
        if any(neg in sentence for neg in _NEGATIONS):
            continue
        for rx in _STUDENT_ONLY_RES:
            if rx.search(sentence):
                return f"students-only listing: {sentence.strip()[:120]!r}"
        for rx in _SENIOR_ONLY_RES:
            if rx.search(sentence):
                return f"seniors-only listing: {sentence.strip()[:120]!r}"
        for rx in _SHORT_STAY_RES:
            m = rx.search(sentence)
            if not m:
                continue
            months = int(m.group(1)) if m.groups() and m.group(1) else 0
            if months == 0 or months < MIN_STAY_MONTHS:
                return (f"short/temporary stay (<{MIN_STAY_MONTHS} months): "
                        f"{sentence.strip()[:120]!r}")
    return None


def passes(listing: RawListing) -> tuple[bool, str]:
    """Return (ok, reason). ok=False means a hard, published fact vetoes it."""
    # City often lives only in the URL path (e.g. huurwoningen's JSON-LD address
    # is just the street), so include source_url in the haystack. This both
    # rescues real listings whose parsed address lacks a locality AND cheaply
    # vetoes other-city listings (e.g. an ikwilhuren /object/delft-... URL)
    # before they cost an LLM-judge call.
    haystack = f"{listing.city} {listing.address} {listing.title} {listing.source_url}".lower()
    if CITIES and haystack.strip() and not any(c in haystack for c in CITIES):
        return False, f"city not in {CITIES}: {(listing.city + ' ' + listing.address).strip()!r}"

    label = f"{listing.listing_type} {listing.title}".lower()
    if any(m in label for m in _ROOM_MARKERS):
        return False, f"looks like a room/share: {label.strip()!r}"

    veto = hard_exclusion(f"{listing.title}\n{listing.description}")
    if veto:
        return False, veto

    if listing.price is None and REQUIRE_KNOWN_PRICE:
        return False, f"price unknown; rent range is €{MIN_RENT:.0f}-€{MAX_RENT:.0f}"
    if listing.price is not None and listing.price < MIN_RENT:
        return False, f"price €{listing.price:.0f} < €{MIN_RENT:.0f}"
    if listing.price is not None and listing.price > MAX_RENT:
        return False, f"price €{listing.price:.0f} > €{MAX_RENT:.0f}"

    if listing.surface is not None and listing.surface < MIN_SURFACE:
        return False, f"surface {listing.surface:.0f}m² < {MIN_SURFACE:.0f}m²"

    return True, "ok"
