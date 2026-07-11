"""The agent run's result contract: AgentResult + outcome parsing.

rc conventions and the OUTCOME: line grammar shared by the apply prompt,
the loop, and every caller that persists or branches on an outcome."""
from __future__ import annotations

import re
from dataclasses import dataclass


# Outcomes the model may declare via a final "OUTCOME: <x>" line.
VALID_OUTCOMES = {
    "submitted", "already_applied", "not_available", "not_eligible",
    "login_required", "blocked", "payment_required",
}

# rc for "the LLM API refused for lack of credit": the agent never really ran,
# so callers must NOT consume the listing (one-attempt rule) — the run said
# nothing about the listing, exactly like a browser-lock timeout.
NO_CREDIT_RC = 126


@dataclass
class AgentResult:
    rc: int            # 0 ok, 1 incomplete/loop, 2 setup error, 124 timeout
    outcome: str       # one of VALID_OUTCOMES, or incomplete/timeout/error/unknown
    summary: str       # the model's final one-paragraph status
    transcript_path: str = ""
    resolved_url: str = ""  # last distinct external URL the browser actually
    # reached, when different from the input source_url -- e.g. an
    # aggregator listing's real destination after in-page redirect dialogs.
    # Callers persist this as an extra dedup key (see
    # dedup.known_processed_urls) so a listing reachable via two different
    # entry points isn't double-submitted.

    @property
    def applied(self) -> bool:
        return self.outcome == "submitted"

    @property
    def terminal(self) -> bool:
        """True when retrying would not help (don't re-attempt this listing)."""
        return self.outcome in VALID_OUTCOMES


_OUTCOME_RE = re.compile(r"OUTCOME:\s*([a-z_]+)", re.IGNORECASE)


def _extract_outcome(text: str) -> str | None:
    """Return the declared outcome if `text` contains the mandatory final
    'OUTCOME: <x>' line from the apply prompt, else None."""
    m = _OUTCOME_RE.search(text or "")
    if m and m.group(1).lower() in VALID_OUTCOMES:
        return m.group(1).lower()
    return None


def _parse_outcome(final_text: str, rc: int) -> str:
    outcome = _extract_outcome(final_text)
    if outcome:
        return outcome
    if rc == NO_CREDIT_RC:
        return "no_credit"
    if rc == 124:
        return "timeout"
    if rc == 2:
        return "error"
    if rc == 1:
        return "incomplete"
    return "unknown"
