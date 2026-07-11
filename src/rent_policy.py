"""Shared rent cap helpers.

The apply stage needs a hard maximum: never apply above the user's rent cap,
even if a listing otherwise looks eligible.
"""
from __future__ import annotations

import re

from .settings import settings

MAX_RENT = settings().max_rent

_PRICE_RE = re.compile(r"(\d[\d.,\s]*)")


def parse_rent(value) -> float | None:
    """Best-effort euro/month parse from strings such as ``€ 1.750,00``."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value)
    if not text.strip() or text.strip() in {"?", "-"}:
        return None

    match = _PRICE_RE.search(text)
    if not match:
        return None

    raw = match.group(1).replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def over_max_rent(value) -> bool:
    price = parse_rent(value)
    return price is not None and price > MAX_RENT
