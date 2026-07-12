"""Conservative text policy for listings that need a separate bedroom.

The policy is deliberately three-valued: only explicit evidence of a single
shared room rejects a listing, and only explicit evidence of a separate
bedroom overrides that signal. Everything else is unknown and passes
through, because a missing or vague description must not create a false
negative application decision.
"""
from __future__ import annotations

import re
from enum import StrEnum


class BedroomLayout(StrEnum):
    SEPARATE = "separate"
    SINGLE_ROOM = "single_room"
    UNKNOWN = "unknown"


# These patterns describe a room arrangement, rather than matching every
# occurrence of words such as "room" or "bedroom". Dutch is the primary
# source language, with common English equivalents for international portals.
_SEPARATE_RE = re.compile(
    r"(?:\b(?:aparte|afzonderlijke|gescheiden|separate|private|privé)"
    r"\s+(?:slaapkamer|slaapruimte|bedroom|sleeping\s+area)\b"
    r"|\b(?:een|één|1|one)\s+(?:slaapkamer|bedroom)\b"
    r"|\b(?:woonkamer|woon|living)\s+en\s+"
    r"(?:slaapkamer|bedroom)\b"
    r"|\b(?:[2-9]|[2-9]\d|two|three|four|five|six|seven|eight|nine)"
    r"\s*[- ]?\s*(?:kamer(?:s)?|rooms?)\b"
    r"|\b(?:[2-9]|[2-9]\d)\s*[- ]?kamer(?:appartement|woning)\b)",
    re.IGNORECASE,
)

_SINGLE_ROOM_RE = re.compile(
    r"(?:\bstudio\b"
    r"|\b(?:een|één|1|one)\s*[- ]?\s*kamer(?:s)?\b"
    r"|\b(?:een|één|1|one)\s*[- ]?\s*kamer"
    r"(?:appartement|woning)\b"
    r"|\b(?:woon|woonkamer|living)\s*(?:\s*(?:-|/)\s*){1,2}"
    r"(?:slaapkamer|slaapruimte|sleeping(?:\s+area)?|bedroom)\b"
    r"|\b(?:alles|everything)\s+in\s+(?:één|een|one)\s+ruimte\b"
    r"|\b(?:sleeping\s+(?:area|nook)\s+in\s+the\s+living\s+room)\b"
    r"|\bliving\s+and\s+sleeping\s+area\b"
    r"|\b(?:slaapvide|sleeping\s+loft|sleeping\s+nook)\b)",
    re.IGNORECASE,
)


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def classify_layout(text: str) -> BedroomLayout:
    """Classify a listing description without guessing from absent evidence."""
    text = _normalise(text)
    if not text:
        return BedroomLayout.UNKNOWN
    # A clear separate-bedroom statement is the explicit exception to a
    # generic "studio" label (some portals use that label loosely).
    if _SEPARATE_RE.search(text):
        return BedroomLayout.SEPARATE
    if _SINGLE_ROOM_RE.search(text):
        return BedroomLayout.SINGLE_ROOM
    return BedroomLayout.UNKNOWN


def disallowed_reason(text: str) -> str | None:
    """Return a short evidence excerpt when the text definitely fails."""
    if classify_layout(text) is not BedroomLayout.SINGLE_ROOM:
        return None
    match = _SINGLE_ROOM_RE.search(_normalise(text))
    return match.group(0) if match else "single shared room"
