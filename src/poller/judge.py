"""LLM judgment for the two filters that need semantic reading, not fields:

  - distance-to-center: is the address within ~15 min cycling of the city
    centre (Utrecht or Amsterdam)?
  - roommates: is this a self-contained home, or a shared/room listing dressed
    up without the word "kamer"?

Runs a cheap model (POLL_JUDGE_MODEL) on OpenRouter, same client as the apply
agent. Fails OPEN: if the model errors or is unsure, the listing PASSES — in
this market a missed home costs more than a wasted look (owner's standing call).
"""
from __future__ import annotations

import json
import os

from openai import AsyncOpenAI

from .models import RawListing

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
JUDGE_MODEL = os.environ.get("POLL_JUDGE_MODEL", "deepseek/deepseek-v4-flash")
MAX_CYCLING_MIN = int(os.environ.get("POLL_MAX_CYCLING_MIN", "15"))

_SYSTEM = (
    "You screen Dutch rental listings for a solo applicant. Answer ONLY with a "
    "compact JSON object: {\"ok\": bool, \"reason\": str}. ok=true means the "
    "listing is worth applying to. Reject (ok=false) only when you are CONFIDENT "
    "of one of these:\n"
    f"1. The address is clearly MORE than {MAX_CYCLING_MIN} minutes cycling from "
    "the city centre of Utrecht or Amsterdam (judge from the "
    "neighbourhood/postcode you recognise).\n"
    "2. It is a SHARED home or single ROOM (roommates, huisgenoten, kamer, "
    "student room), judged from the whole description, not just the word 'kamer'.\n"
    "If you are unsure, or lack the info, answer ok=true. Never reject for any "
    "other reason (price, size, etc. are handled elsewhere)."
)


def _client() -> AsyncOpenAI | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    return AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=key)


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
        "type": listing.listing_type,
    }, ensure_ascii=False)

    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            extra_body={"reasoning": {"enabled": False}},
            max_tokens=200,
            temperature=0,
        )
        content = (resp.choices[0].message.content or "").strip()
        content = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(content)
        ok = bool(data.get("ok", True))
        return ok, str(data.get("reason", ""))[:200]
    except Exception as e:  # noqa: BLE001 - never let the judge block the pipeline
        return True, f"judge error, fail-open: {type(e).__name__}: {e}"
