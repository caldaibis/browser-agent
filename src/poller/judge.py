"""LLM judgment for filters that need semantic reading, not just raw fields:

  - distance-to-center: is the address within ~15 min cycling of the city
    centre (Utrecht or Amsterdam)?
  - roommates: is this a self-contained home, or a shared/room listing dressed
    up without the word "kamer"?
  - price/surface: does the listing clearly fit the configured rent band and
    minimum square meters when the raw parser fields are sparse or ambiguous?

Runs the judge model (POLL_JUDGE_MODEL) on OpenRouter, same client as the apply
agent. Fails OPEN: if the model errors or is unsure, the listing PASSES — in
this market a missed home costs more than a wasted look (owner's standing call).
"""
from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

from ..rent_policy import MAX_RENT
from .models import RawListing

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
JUDGE_MODEL = os.environ.get("POLL_JUDGE_MODEL", "deepseek/deepseek-v4-pro")
MAX_CYCLING_MIN = int(os.environ.get("POLL_MAX_CYCLING_MIN", "15"))
MIN_RENT = float(os.environ.get("POLL_MIN_PRICE", "800"))
MIN_SURFACE = float(os.environ.get("POLL_MIN_SURFACE", "30"))

_SYSTEM = (
    "You screen Dutch rental listings for a solo applicant. Answer ONLY with a "
    "compact JSON object: {\"ok\": bool, \"reason\": str}. ok=true means the "
    "listing is worth applying to. Reject (ok=false) when you are CONFIDENT of "
    "one of these:\n"
    f"1. The address is clearly MORE than {MAX_CYCLING_MIN} minutes cycling from "
    "the city centre of Utrecht or Amsterdam (judge from the "
    "neighbourhood/postcode you recognise).\n"
    "2. It is a SHARED home or single ROOM (roommates, huisgenoten, kamer, "
    "student room), judged from the whole description, not just the word 'kamer'.\n"
    f"3. The rent is clearly outside EUR {MIN_RENT:.0f}-{MAX_RENT:.0f} per month. "
    "Treat service costs/inclusive rent conservatively: reject only when the "
    "monthly rent is clearly below the minimum or above the maximum.\n"
    f"4. The living area is clearly below {MIN_SURFACE:.0f} m2.\n"
    "Use the structured fields first. Also read the title/address/url/type for "
    "obvious clues. If price or surface is unknown or ambiguous, do not reject "
    "on that criterion. If you are unsure overall, answer ok=true. Never reject "
    "for any other reason."
)


def _client() -> AsyncOpenAI | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    return AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)


def _ok_value(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"false", "no", "0", "reject"}
    return bool(value)


async def judge(listing: RawListing, model: str = JUDGE_MODEL) -> tuple[bool, str]:
    """Return (ok, reason). Fails open (True) on any error/uncertainty."""
    client = _client()
    if client is None:
        return True, "no OPENROUTER_API_KEY; fail-open"

    user = json.dumps({
        "url": listing.source_url,
        "title": listing.title,
        "address": listing.address,
        "city": listing.city,
        "price_eur_per_month": listing.price,
        "surface_m2": listing.surface,
        "type": listing.listing_type,
    }, ensure_ascii=False)

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            extra_body={"reasoning": {"enabled": False}},
            max_tokens=300,
            temperature=0,
        )
        content = (resp.choices[0].message.content or "").strip()
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(content)
        ok = _ok_value(data.get("ok", True))
        return ok, str(data.get("reason", ""))[:200]
    except Exception as e:  # noqa: BLE001 - never let the judge block the pipeline
        return True, f"judge error, fail-open: {type(e).__name__}: {e}"
