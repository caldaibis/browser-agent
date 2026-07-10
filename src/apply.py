"""Apply stage: hand the external listing to our browser agent loop.

Builds a precise task prompt (source URL, reference message, document list,
auto-submit instruction) and runs the lightweight agent loop in
`src.browser_agent` (DeepSeek LLM + Playwright MCP over our shared CDP
browser). The agent adapts to whatever application form the source site shows.

Run standalone:  python -m src.apply logs/last_listing.json
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from . import known_gates, site_playbooks
from .apply_priority import priority_pending
from .config import LOG_DIR, CDP_URL
from .listing_context import fetch_context
from .message_template import REFERENCE_APPLICATION_MESSAGE
from .browser_agent import run_agent, AgentResult
from .poller.browser_lock import browser_lock
from .rent_policy import MAX_RENT, parse_rent
from .eventlog import get_logger
from .models import Listing
from .prompts import build_prompt
from .settings import settings
from .site_fastpaths import try_fast_apply

_LOG = get_logger("apply")

# Model for the apply agent. Override via APPLY_MODEL.
APPLY_MODEL = settings().apply_model
APPLY_MAX_TURNS = settings().apply_max_turns
APPLY_TIMEOUT_SECONDS = settings().apply_timeout_seconds
APPLY_FASTPATH_ENABLED = settings().apply_fastpath_enabled

# Google account used for "Sign in with Google" SSO on source sites (Funda etc.).
GOOGLE_ACCOUNT = settings().google_account

# Sites/wording where applying is gated by a paid registration or membership.
# These are not normal free rental forms, and the bot must not spend money or
# spend LLM turns discovering a checkout. your-house.nl was verified on
# 07-07-2026 to lead to a live Mollie EUR 25 membership payment before applying.
KNOWN_PAID_APPLICATION_DOMAINS = {"your-house.nl"}
_PAYMENT_SENTENCE_SPLIT = re.compile(r"[.!?\n|]+")
_PAYMENT_NEGATIONS = ("geen", "niet", "no ", "not ", "gratis", "free")
_PAYMENT_TEXT_RES = (
    re.compile(
        r"\b(lidmaatschap|inschrijfkosten|registratiekosten|servicekosten)\b"
        r"[^.!?\n]{0,120}(€\s*\d|eur\s*\d|\d+[\d.,]*\s*euro)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(€\s*\d[\d.,]*|eur\s*\d[\d.,]*|\d+[\d.,]*\s*euro)"
        r"[^.!?\n]{0,120}\b(lidmaatschap|inschrijven|registratie|reageren)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(paywall|upgrade required|plus account required)\b", re.IGNORECASE),
)


def _domain(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _payment_required_reason(listing: Listing) -> str | None:
    domain = _domain(listing.source_url)
    if domain in KNOWN_PAID_APPLICATION_DOMAINS:
        return f"{domain} is a known paid-registration application site"

    # Gates recorded at diagnosis time by the self-improvement agent
    # (state/known_gates.json) — same veto as the hardcoded set above, but
    # updatable at runtime without a deploy. Fail-open.
    gate_reason = known_gates.paid_registration_reason(listing.source_url)
    if gate_reason:
        return gate_reason

    description = listing.description.strip()
    if not description:
        ctx = fetch_context(listing.source_url)
        if ctx:
            description = ctx.description
    text = "\n".join(
        (listing.source_name, listing.address, listing.title, description))
    for sentence in _PAYMENT_SENTENCE_SPLIT.split(text):
        low = sentence.lower()
        if not low.strip() or any(n in low for n in _PAYMENT_NEGATIONS):
            continue
        for rx in _PAYMENT_TEXT_RES:
            if rx.search(sentence):
                return f"paid registration/application wording: {sentence.strip()[:180]}"
    return None


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return (s or "listing")[:50]


def apply(listing: Listing | dict, model: str = APPLY_MODEL,
          yield_to_priority: bool = False) -> AgentResult:
    """Run the apply agent on one listing. Returns an AgentResult with the true
    outcome (submitted / already_applied / not_available / ... / timeout).

    yield_to_priority: set by the poller's applier. The run then checks the
    mail-apply priority flag once per turn and aborts with outcome "yielded"
    (listing untouched, caller requeues) so a time-critical mail-triggered
    apply gets the shared browser within seconds. Mail/manual runs ARE the
    priority path and leave this off."""
    if not isinstance(listing, Listing):
        listing = Listing.from_json(listing)
    listing_price = parse_rent(listing.price)
    if listing_price is not None and listing_price > MAX_RENT:
        summary = (
            f"Skipped before opening the browser: listed rent €{listing_price:.0f} "
            f"is above the configured max rent €{MAX_RENT:.0f}."
        )
        _LOG.info(f"{summary}")
        return AgentResult(rc=0, outcome="not_eligible", summary=summary)

    payment_reason = _payment_required_reason(listing)
    if payment_reason:
        summary = (
            "Skipped before opening the browser: applying or registering "
            f"requires payment ({payment_reason}). I did not pay."
        )
        _LOG.info(f"{summary}")
        return AgentResult(rc=0, outcome="payment_required", summary=summary)

    prompt = build_prompt(listing)
    # Persist a per-run transcript + prompt so nothing is overwritten/lost.
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    slug = _slug(f"{listing.source_name}-{listing.address}")
    run_dir = LOG_DIR / "transcripts"
    run_dir.mkdir(parents=True, exist_ok=True)
    transcript = run_dir / f"{ts}_{slug}.log"
    (run_dir / f"{ts}_{slug}.prompt.txt").write_text(prompt, encoding="utf-8")
    (LOG_DIR / "last_apply_prompt.txt").write_text(prompt, encoding="utf-8")

    _LOG.info(f"launching agent ({model}) for {listing.source_url}")
    _LOG.info(f"transcript: {transcript}")
    # Exclusive browser lock: only one component drives the shared CDP browser
    # at a time. Coordinates the Stekkies orchestrator and the poller's applier.
    with browser_lock(holder=f"apply:{slug}"):
        result = None
        if APPLY_FASTPATH_ENABLED:
            result = try_fast_apply(
                listing=listing.to_json(),
                cdp_url=CDP_URL,
                log_path=transcript,
                message=REFERENCE_APPLICATION_MESSAGE.replace(
                    "[[ADDRESS]]", listing.address or "de woning"),
            )
        if result is None:
            result = run_agent(
                prompt=prompt,
                model=model,
                max_turns=APPLY_MAX_TURNS,
                cdp_url=CDP_URL,
                log_path=transcript,
                timeout_seconds=APPLY_TIMEOUT_SECONDS,
                source_url=listing.source_url,
                yield_check=priority_pending if yield_to_priority else None,
            )
    # Keep the convenience "latest" copy too.
    try:
        (LOG_DIR / "last_apply_output.txt").write_text(
            transcript.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass
    _LOG.info(f"----- agent finished: outcome={result.outcome} (rc={result.rc}) -----")
    result.transcript_path = str(transcript)
    # Distill durable site knowledge out of this run for the next one on the
    # same domain(s). Fail-open and outside the browser lock — see the module.
    site_playbooks.update_after_run(listing, result)
    return result


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else LOG_DIR / "last_listing.json"
    listing = Listing.from_json(json.loads(path.read_text(encoding="utf-8")))
    result = apply(listing)
    print(f"OUTCOME: {result.outcome}")
    return 0 if result.rc == 0 else result.rc


if __name__ == "__main__":
    sys.exit(main())
