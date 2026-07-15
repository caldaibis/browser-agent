"""Deterministic in-loop guards — every one exists because prompt text
alone verifiably failed at the same job (see docs/lessons/): context
pruning, the hard money guard, mid-form grace turns, loop/cycle
detection, and snapshot-overuse nudging."""
from __future__ import annotations

import re

from ..settings import settings

# Stale-page-dump pruning. Full-page tool results (browser_snapshot,
# browser_navigate's embedded snapshot, dom_scan) run ~7k tokens each and the
# model only ever acts on the newest one -- but every one appended to
# `messages` is re-sent on every later turn, so cumulative input grows
# quadratically with turns. Measured on the 60-turn Hof van Oslo transcript
# (20260701_144029): context grew 7.7k -> 188k tokens and the run consumed
# 6.12M cumulative prompt tokens, ~all of it re-sent stale page dumps. So:
# each turn, every large tool result except the newest PRUNE_KEEP_RECENT is
# replaced in-place with a short stub. Each prune invalidates DeepSeek's
# prefix cache from the stubbed message onward, but the stub lands near the
# tail (the 3rd-newest dump), so the one-off miss re-read is far smaller than
# carrying ~7k extra tokens on every remaining turn.
PRUNE_MIN_CHARS = settings().apply_prune_min_chars
PRUNE_KEEP_RECENT = settings().apply_prune_keep_recent
STALE_DUMP_STUB = (
    "[stale page dump removed to save context. The page may have changed "
    "since; rely on the most recent snapshot/scan, or take a fresh one if "
    "you need the current state.]"
)

# Cap on any single tool result fed back to the model. Long pages blow past
# this, and a silent cut makes the model conclude something is absent when it
# was merely below the cut — so truncation is always marked as such.
TOOL_RESULT_MAX_CHARS = 20000


def _clamp_tool_result(text: str) -> str:
    if len(text) <= TOOL_RESULT_MAX_CHARS:
        return text
    return text[:TOOL_RESULT_MAX_CHARS] + (
        f"\n[tool output truncated at {TOOL_RESULT_MAX_CHARS} chars -- the "
        "page continues beyond this point. Do NOT conclude an element is "
        "absent because it isn't shown above; scroll to it or target it "
        "directly instead.]"
    )


def _prune_stale_page_dumps(messages: list[dict]) -> int:
    """Replace all but the newest PRUNE_KEEP_RECENT large tool results with
    STALE_DUMP_STUB (in place, tool_call_id preserved). Returns how many
    messages were stubbed this call. Already-stubbed messages fall under the
    size threshold, so repeat calls are no-ops for them."""
    big_idx = [
        i for i, m in enumerate(messages)
        if m.get("role") == "tool"
        and len(m.get("content") or "") >= PRUNE_MIN_CHARS
    ]
    for i in big_idx[:-PRUNE_KEEP_RECENT]:
        messages[i]["content"] = STALE_DUMP_STUB
    return len(big_idx[:-PRUNE_KEEP_RECENT])

# HARD MONEY GUARD. The agent must never spend real money. On 07-07-2026 a run
# navigated all the way to a live Mollie €25 "membership" checkout on
# your-house.nl and spent a whole turn *deliberating whether to pay* before
# declining — one tool call from entering payment details. Prompt text is not
# a spending boundary, so this is enforced in code: the moment the browser's
# current tab lands on a known payment processor / checkout, the run aborts
# with payment_required BEFORE the model can act on that page. Substring match
# on the host (adyen/stripe expose checkout on subdomains).
_PAYMENT_HOST_MARKERS = (
    "mollie.com", "buckaroo.nl", "buckaroo.eu", "adyen.com", "stripe.com",
    "checkout.stripe", "paypal.com", "pay.nl", "ideal.nl", "targetpay",
    "sisow", "multisafepay", "worldline", "ingenico",
)


def _is_payment_url(url: str) -> bool:
    host = (re.sub(r"^https?://", "", url or "").split("/", 1)[0]).lower()
    return any(m in host for m in _PAYMENT_HOST_MARKERS)

# One-shot extra turn budget granted at max_turns when the run is
# demonstrably mid-form (recent fill/upload/select activity): killing a run
# that is one dropdown away from submitting consumes the listing forever
# under the one-attempt rule. Verified twice in production (03/05-07-2026):
# a run died at turn 60 while dismissing a cookie overlay blocking the LAST
# form field, another died at turn 60 right after finally locating the real
# listing URL. Wall-clock APPLY_TIMEOUT_SECONDS still bounds the whole run.
GRACE_TURNS = settings().apply_grace_turns
_FORM_TOOLS = {
    "browser_fill_form", "browser_type", "browser_file_upload",
    "browser_select_option", "fill_by_label", "select_option_by_label",
    "check_by_label",
}


def _recent_form_activity(sig_history: list[tuple], window: int = 8) -> bool:
    """True when any of the last `window` turns' tool calls touched a form —
    the signal that the run is mid-application rather than lost/looping."""
    for sig in sig_history[-window:]:
        for name, _ in sig:
            if name in _FORM_TOOLS:
                return True
    return False


def _trailing_cycle_repeats(history: list[tuple], period: int) -> int:
    """How many consecutive times the last `period` actions repeat the
    `period` actions before them. 0 if the tail isn't such a cycle.

    period=1 catches exact repeats (e.g. ArrowDown x30, the original case).
    period=2/3 catches short oscillations that never repeat the *same*
    single action back-to-back but still make no progress — e.g. click a
    button / Escape a dialog it opened / click the same button again / Escape
    again, forever. This looks "different" turn-to-turn (different sig each
    time) so the period=1 check alone never fires, but the 2-action pattern
    itself is repeating.
    """
    reps = 0
    while True:
        start = len(history) - period * (reps + 2)
        if start < 0:
            break
        if history[start:start + period] == history[start + period:start + period * 2]:
            reps += 1
        else:
            break
    return reps


def _should_nudge_snapshot_overuse(snapshot_calls: int, turn: int) -> bool:
    """True once browser_snapshot has dominated the turns so far despite the
    prompt's own 'don't re-snapshot after every click' guidance -- verified in
    production to not be reliably followed on its own (Hof van Oslo,
    01-07-2026: ~29 of 60 turns were snapshots, each following a *different*
    click, so the exact/short-cycle repeat guard never fires -- the repeated
    element is the call TYPE, not its arguments). One-shot course-correction,
    not a hard cap.
    """
    return turn >= 10 and snapshot_calls >= max(6, turn // 2)
