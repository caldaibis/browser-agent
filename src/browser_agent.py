"""Minimal browser agent loop — our lightweight replacement for Hermes.

Connects to the Playwright MCP server (stdio) attached to our shared CDP browser,
exposes its tools to a DeepSeek-hosted LLM, and runs a tool-calling loop until
the model produces a final text answer or the turn budget is hit.

What this gives us over Hermes: full control, our logging, no 1.2 GB harness —
just the agentic loop + MCP client. The Playwright MCP (the genuinely valuable
piece: snapshot/click/fill_form/file_upload) is unchanged.

Env:
  DEEPSEEK_API_KEY   required — your DeepSeek key.

Public API:
  run_agent(...) -> AgentResult  (rc, outcome, summary)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI, APIStatusError
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import browser_dom_tools, credentials
from .poller import dedup as poller_dedup
from .self_improvement_harness import record_trajectory_event

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# Reasoning control. DeepSeek thinking mode can burn hidden reasoning
# tokens before emitting content/tool_calls. Over a large page snapshot that can
# hit the completion cap mid-reasoning (finish_reason="length", empty content,
# no tool_calls) and look like a stall. Filling a rental form is not rocket
# science, so we DISABLE reasoning by default. Override via
# APPLY_REASONING_EFFORT = off | low | medium | high | max (anything but
# "off" re-enables reasoning at that effort).
REASONING_EFFORT = os.environ.get("APPLY_REASONING_EFFORT", "off").lower()
if REASONING_EFFORT == "minimal":
    REASONING_EFFORT = "low"
THINKING = (
    {"type": "disabled"}
    if REASONING_EFFORT in ("off", "none", "false", "0")
    else {"type": "enabled"}
)

# Completion cap per turn. With reasoning off the visible output (a tool call or a
# short status) is small, so this is just generous headroom against truncation.
MAX_TOKENS = int(os.environ.get("APPLY_MAX_TOKENS", "8000"))

# Local (non-MCP) tool: look up a stored site login by domain/URL on demand, so
# credentials never sit in the prompt and the agent can fetch whichever site it
# actually lands on (a single application can span multiple hosts).
CREDENTIAL_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_credential",
        "description": (
            "Return the stored username/password for a rental site. Pass the "
            "site's domain or current URL (e.g. 'ikwilhuren.nu'). Use this for "
            "every email/password login instead of guessing; returns an error "
            "string if no credential is stored for that site. Some listing "
            "sites redirect their login to a SHARED third-party auth provider "
            "(e.g. eye-move.nl / mijnklantdossier.nl) that also serves other, "
            "unrelated rental sites with DIFFERENT accounts — credentials are "
            "stored per originating listing site, not per shared provider. If "
            "the current login page's own domain has no stored credential, "
            "retry this tool with THIS listing's original domain (from the "
            "'Apply at this URL' line at the top of your task) instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "site": {
                    "type": "string",
                    "description": "Domain or full URL of the login page, e.g. 'ikwilhuren.nu'.",
                },
            },
            "required": ["site"],
        },
    },
}

# Local (non-MCP) fallback tools for when the Playwright MCP's accessibility-
# tree snapshot doesn't show something known to be on the page -- seen
# repeatedly on real listings: an HTML dialog/overlay built without proper
# ARIA roles never gets a browser_snapshot ref, so browser_click can't target
# it and browser_handle_dialog doesn't apply (that's for native JS dialogs,
# not in-page HTML). These query the raw DOM / click by visible text instead
# of the accessibility tree -- narrow, fixed operations, NOT arbitrary JS
# (BLOCKED_TOOLS below still applies).
DOM_SCAN_TOOL = {
    "type": "function",
    "function": {
        "name": "dom_scan",
        "description": (
            "FALLBACK ONLY. Raw-DOM page report (title/url/text + every "
            "button, link, and form field found by direct DOM query) -- NOT "
            "the accessibility tree browser_snapshot uses. Use this ONLY when "
            "you know something is on the page (e.g. you just clicked a "
            "button that should open a dialog/modal) but browser_snapshot "
            "doesn't show it. Waits briefly first so a just-opened dialog has "
            "time to render. Do NOT use this as your primary way to read the "
            "page -- prefer browser_snapshot; this is slower and has no refs, "
            "only visible text."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}
CLICK_BY_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "click_by_text",
        "description": (
            "FALLBACK ONLY. Click the first element whose VISIBLE TEXT "
            "matches (not a browser_snapshot ref). Use this ONLY when "
            "dom_scan shows an element you need to click (e.g. inside a "
            "dialog invisible to browser_snapshot) but it has no ref you can "
            "pass to browser_click. Do NOT use this instead of browser_click "
            "for anything a snapshot ref already covers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Visible text of the element to click, e.g. 'Ja, ik ga akkoord'.",
                },
            },
            "required": ["text"],
        },
    },
}
FILL_BY_LABEL_TOOL = {
    "type": "function",
    "function": {
        "name": "fill_by_label",
        "description": (
            "FALLBACK ONLY. Type into the text/email/tel/textarea input "
            "associated with the given <label> text, bypassing the "
            "accessibility-tree ref system. Use this ONLY when dom_scan shows "
            "a form field inside a dialog invisible to browser_snapshot -- "
            "such a field has no ref, so browser_type/browser_fill_form "
            "cannot reach it at all. Do NOT use this instead of "
            "browser_type/browser_fill_form for anything a snapshot ref "
            "already covers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Visible label text of the field, e.g. 'Voornaam'.",
                },
                "value": {
                    "type": "string",
                    "description": "Text to type into that field.",
                },
            },
            "required": ["label", "value"],
        },
    },
}
SELECT_OPTION_BY_LABEL_TOOL = {
    "type": "function",
    "function": {
        "name": "select_option_by_label",
        "description": (
            "FALLBACK ONLY. Operate a custom (non-<select>) dropdown inside a "
            "dialog invisible to browser_snapshot: opens the dropdown "
            "associated with the given label, then clicks the option matching "
            "the given visible text. Use this ONLY for a dropdown dom_scan "
            "shows with no ref -- e.g. one where the toggle control has no "
            "text of its own (an icon only), so click_by_text can't target it "
            "either. Do NOT use this for a normal <select> or any dropdown a "
            "snapshot ref already covers -- use browser_select_option there."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Visible label text of the dropdown field, e.g. 'Soort inkomen'.",
                },
                "option": {
                    "type": "string",
                    "description": "Visible text of the option to select, e.g. 'Loondienst'.",
                },
            },
            "required": ["label", "option"],
        },
    },
}

# Playwright MCP tools we never want the model to use (raw JS = token bleed).
BLOCKED_TOOLS = {"browser_evaluate", "browser_run_code_unsafe"}

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
PRUNE_MIN_CHARS = int(os.environ.get("APPLY_PRUNE_MIN_CHARS", "2500"))
PRUNE_KEEP_RECENT = max(1, int(os.environ.get("APPLY_PRUNE_KEEP_RECENT", "2")))
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

# Outcomes the model may declare via a final "OUTCOME: <x>" line.
VALID_OUTCOMES = {
    "submitted", "already_applied", "not_available", "not_eligible",
    "login_required", "blocked", "payment_required",
}

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
GRACE_TURNS = int(os.environ.get("APPLY_GRACE_TURNS", "10"))
_FORM_TOOLS = {
    "browser_fill_form", "browser_type", "browser_file_upload",
    "browser_select_option", "fill_by_label", "select_option_by_label",
}

# Deterministic cookie-banner dismissal after every navigation (free — no LLM
# turn). Cookie/consent overlays intercept clicks and eat turns: one observed
# run burned its final turns clicking a consent modal away field by field.
AUTO_COOKIE = os.environ.get("APPLY_AUTO_COOKIE", "1") != "0"

# rc for "the LLM API refused for lack of credit": the agent never really ran,
# so callers must NOT consume the listing (one-attempt rule) — the run said
# nothing about the listing, exactly like a browser-lock timeout.
NO_CREDIT_RC = 126

def _recent_form_activity(sig_history: list[tuple], window: int = 8) -> bool:
    """True when any of the last `window` turns' tool calls touched a form —
    the signal that the run is mid-application rather than lost/looping."""
    for sig in sig_history[-window:]:
        for name, _args in sig:
            if name in _FORM_TOOLS:
                return True
    return False


@dataclass
class AgentResult:
    rc: int            # 0 ok, 1 incomplete/loop, 2 setup error, 124 timeout,
    #                    125 yielded to a priority (mail-triggered) apply
    outcome: str       # one of VALID_OUTCOMES, or incomplete/timeout/error/unknown
    summary: str       # the model's final one-paragraph status
    transcript_path: str = ""
    resolved_url: str = ""  # last distinct external URL the browser actually
    # reached, when different from the input source_url -- e.g. an
    # aggregator listing's real destination after in-page redirect dialogs.
    # Callers persist this as an extra dedup key (see
    # poller.dedup.known_processed_urls) so a listing reachable via two
    # different entry points isn't double-submitted.

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
    if rc == 125:
        return "yielded"
    if rc == 124:
        return "timeout"
    if rc == 2:
        return "error"
    if rc == 1:
        return "incomplete"
    return "unknown"


def _mcp_params(cdp_url: str) -> StdioServerParameters:
    return StdioServerParameters(
        command="npx",
        args=[
            "-y", "@playwright/mcp@latest",
            "--cdp-endpoint", cdp_url,
            "--allow-unrestricted-file-access",
        ],
    )


def _to_openai_tools(mcp_tools) -> list[dict]:
    out = []
    for t in mcp_tools:
        if t.name in BLOCKED_TOOLS:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": (t.description or "")[:1024],
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        })
    return out


def _result_text(result) -> str:
    """Flatten an MCP CallToolResult into text for the model."""
    parts = []
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
        else:
            parts.append(str(block))
    return "\n".join(parts) if parts else "(no output)"


_CURRENT_TAB_RE = re.compile(r"^- \d+: \(current\).*\((https?://[^)]+)\)\s*$", re.MULTILINE)


async def _current_tab_url(session: "ClientSession") -> str | None:
    """Ask the MCP itself which tab is current (browser_tabs marks it with
    "(current)") so dom_scan/click_by_text -- which connect over CDP on a
    separate Playwright client and can't see the MCP's own tab pointer --
    look at the same tab the model has been looking at, not just the
    last-created one. See browser_dom_tools.current_page for why that
    fallback is unreliable here."""
    try:
        result = await session.call_tool("browser_tabs", {"action": "list"})
    except Exception:
        return None
    m = _CURRENT_TAB_RE.search(_result_text(result))
    return m.group(1) if m else None


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


def _record_trajectory(run_id: str, event: str, payload: dict | None = None) -> None:
    if run_id:
        record_trajectory_event(run_id, event, payload or {})


async def _run(prompt: str, model: str, max_turns: int, cdp_url: str, log: "Logger",
                source_url: str = "", resolved: dict | None = None,
                yield_check=None, trajectory_id: str = "") -> tuple[int, str]:
    """resolved, when given, is a plain dict this fills in as {"url": ...} with
    the last distinct external destination the browser actually reached --
    read back by run_agent() after the call so callers can persist it as an
    extra dedup key (see poller_dedup.known_processed_urls).

    yield_check, when given, is a zero-arg callable polled once per turn
    (before spending the LLM call): when it returns True the run aborts with
    rc=125 / outcome "yielded" so a higher-priority apply (a mail-triggered
    one, see apply_priority.py) can take the shared browser within seconds
    instead of waiting out this whole run. A yielded attempt is NOT a verdict
    on the listing -- callers requeue it untouched."""
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        log.line("[agent] ERROR: DEEPSEEK_API_KEY not set")
        return 2, "DEEPSEEK_API_KEY not set."

    source_canon = poller_dedup.canonical_url(source_url) if source_url else None
    known_urls = poller_dedup.known_processed_urls()

    client = AsyncOpenAI(base_url=DEEPSEEK_BASE_URL, api_key=api_key)

    async with stdio_client(_mcp_params(cdp_url)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = _to_openai_tools(mcp_tools) + [
                CREDENTIAL_TOOL, DOM_SCAN_TOOL, CLICK_BY_TEXT_TOOL,
                FILL_BY_LABEL_TOOL, SELECT_OPTION_BY_LABEL_TOOL,
            ]
            log.line(f"[agent] model={model} tools={len(tools)} cdp={cdp_url}")
            _record_trajectory(trajectory_id, "run_start", {
                "model": model,
                "tool_count": len(tools),
                "source_url": source_url,
                "max_turns": max_turns,
            })

            messages: list[dict] = [{"role": "user", "content": prompt}]
            nudges_left = 2  # if the model stops early without finishing, prod it
            trunc_retries_left = 4  # tolerate transient truncated/empty completions
            sig_history: list[tuple] = []  # detect degenerate repeated-action loops
            repeat_nudged = False
            snapshot_calls = 0  # detect excessive re-snapshotting (see _should_nudge_snapshot_overuse)
            snapshot_nudged = False
            turn = 0
            budget = max_turns
            grace_granted = False
            while True:
                if turn >= budget:
                    if not grace_granted and _recent_form_activity(sig_history):
                        grace_granted = True
                        budget += GRACE_TURNS
                        log.line(f"[agent] turn budget reached mid-form -- "
                                 f"granting {GRACE_TURNS} grace turns (once)")
                        _record_trajectory(trajectory_id, "guard", {
                            "name": "grace_turns",
                            "turn": turn,
                            "extra_turns": GRACE_TURNS,
                        })
                        messages.append({
                            "role": "user",
                            "content": (
                                f"Your turn budget is nearly exhausted; you have "
                                f"{GRACE_TURNS} extra turns as a one-time grace "
                                "because you are mid-form. Finish the remaining "
                                "fields and SUBMIT now, taking the shortest "
                                "possible path. If you cannot submit within "
                                "these turns, stop and report the exact "
                                "blocking reason with the mandatory "
                                "'OUTCOME: <x>' line."
                            ),
                        })
                    else:
                        break
                turn += 1
                if yield_check is not None and yield_check():
                    log.line(f"[agent] YIELD at turn {turn}: a priority "
                             "(mail-triggered) apply is waiting for the browser")
                    _record_trajectory(trajectory_id, "final", {
                        "rc": 125,
                        "outcome": "yielded",
                        "turn": turn,
                    })
                    return 125, (
                        "Yielded the browser mid-run to a priority mail-triggered "
                        "apply. This attempt did not finish and says nothing about "
                        "the listing itself; it must be re-run."
                    )
                request = {
                    "model": model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_tokens": MAX_TOKENS,
                    "extra_body": {"thinking": THINKING},
                }
                if THINKING["type"] == "enabled":
                    request["reasoning_effort"] = REASONING_EFFORT
                try:
                    resp = await client.chat.completions.create(**request)
                except APIStatusError as e:
                    # Out of API credit is not a verdict on the listing: the
                    # agent can't run at all. Surface it as its own outcome so
                    # callers DON'T consume the listing (one-attempt rule) --
                    # otherwise every listing that drops during a credit
                    # outage is permanently burned as "error".
                    if e.status_code == 402 or "insufficient balance" in str(e).lower():
                        log.line(f"[agent] NO CREDIT: the LLM API refused "
                                 f"(HTTP {e.status_code}): {e}")
                        return NO_CREDIT_RC, (
                            "The DeepSeek API refused the request for lack of "
                            "credit (HTTP 402). No attempt was made on the "
                            "listing; top up and it can be retried."
                        )
                    raise
                choice = resp.choices[0]
                msg = choice.message
                tool_calls = msg.tool_calls or []
                finish_reason = choice.finish_reason

                # Reasoning *text* is hidden, but the *count* is reported. Log it
                # so we can tell "thought too much" (high reasoning_tokens, near the
                # cap) from "cap too low" (finish_reason=length at modest counts).
                usage = getattr(resp, "usage", None)
                ctd = getattr(usage, "completion_tokens_details", None)
                ptd = getattr(usage, "prompt_tokens_details", None)
                prompt_tok = getattr(usage, "prompt_tokens", None)
                reasoning_tok = getattr(ctd, "reasoning_tokens", None)
                completion_tok = getattr(usage, "completion_tokens", None)
                total_tok = getattr(usage, "total_tokens", None)
                cache_hit_tok = (
                    getattr(usage, "prompt_cache_hit_tokens", None)
                    or getattr(ptd, "cached_tokens", None)
                )
                cache_miss_tok = getattr(usage, "prompt_cache_miss_tokens", None)
                log.line(f"[agent] turn {turn} finish={finish_reason} "
                         f"prompt_tokens={prompt_tok} "
                         f"completion_tokens={completion_tok} "
                         f"total_tokens={total_tok} "
                         f"reasoning_tokens={reasoning_tok} "
                         f"cache_hit_tokens={cache_hit_tok} "
                         f"cache_miss_tokens={cache_miss_tok} "
                         f"(cap={MAX_TOKENS})")
                _record_trajectory(trajectory_id, "turn_usage", {
                    "turn": turn,
                    "finish_reason": finish_reason,
                    "prompt_tokens": prompt_tok,
                    "completion_tokens": completion_tok,
                    "total_tokens": total_tok,
                    "reasoning_tokens": reasoning_tok,
                    "cache_hit_tokens": cache_hit_tok,
                    "cache_miss_tokens": cache_miss_tok,
                })

                # A turn cut off mid-reasoning comes back as finish_reason="length"
                # with empty content and no tool_calls — a truncation glitch. We've
                # also seen finish_reason="stop" with empty content and no
                # tool_calls at the same completion-token cost as the model's own
                # successful bare-arg tool calls elsewhere in the same transcript —
                # almost certainly a tool call the provider failed to surface, not a
                # deliberate conclusion (the prompt requires a non-empty OUTCOME
                # line, which empty content can never satisfy). Treat both as the
                # same transport glitch: retry (don't spend a nudge / declare done).
                if not tool_calls and not (msg.content or "").strip() \
                        and finish_reason in ("length", "stop") and trunc_retries_left > 0:
                    trunc_retries_left -= 1
                    log.line(f"[agent] turn {turn} truncated/dropped "
                             f"(finish_reason={finish_reason}, empty); retrying "
                             f"({trunc_retries_left} left)")
                    _record_trajectory(trajectory_id, "guard", {
                        "name": "truncated_empty_retry",
                        "turn": turn,
                        "finish_reason": finish_reason,
                        "retries_left": trunc_retries_left,
                    })
                    # The empty assistant turn was never appended (we continue
                    # before recording it), so just add a prod and retry.
                    messages.append({
                        "role": "user",
                        "content": (
                            "Your previous turn was cut off before you produced an "
                            "answer. Keep reasoning brief and emit your next tool "
                            "call (or final answer) now."
                        ),
                    })
                    continue

                # Record the assistant turn (text + any tool calls).
                assistant_entry: dict = {"role": "assistant", "content": msg.content or ""}
                reasoning_content = getattr(msg, "reasoning_content", None)
                if reasoning_content:
                    assistant_entry["reasoning_content"] = reasoning_content
                if tool_calls:
                    assistant_entry["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in tool_calls
                    ]
                messages.append(assistant_entry)

                if msg.content:
                    log.line(f"[agent] turn {turn} say: {msg.content.strip()[:400]}")
                    _record_trajectory(trajectory_id, "assistant_text", {
                        "turn": turn,
                        "text": msg.content.strip()[:1000],
                    })

                if not tool_calls:
                    content = (msg.content or "").strip()
                    # The prompt mandates the final answer end with an exact
                    # 'OUTCOME: <x>' line. Anything lacking that (empty content,
                    # a raw page/snapshot dump, or narration about an intended
                    # action that never happened) is not a usable conclusion.
                    if _extract_outcome(content) is None:
                        if nudges_left > 0:
                            nudges_left -= 1
                            log.line(f"[agent] nudge (model stopped without a "
                                     f"valid OUTCOME line, finish_reason="
                                     f"{finish_reason}; {nudges_left} left)")
                            _record_trajectory(trajectory_id, "guard", {
                                "name": "missing_outcome_nudge",
                                "turn": turn,
                                "finish_reason": finish_reason,
                                "nudges_left": nudges_left,
                            })
                            messages.append({
                                "role": "user",
                                "content": (
                                    "That was not a usable final answer. If you have "
                                    "ALREADY submitted (saw a confirmation), reply with a "
                                    "one-line success confirmation and nothing else — do "
                                    "NOT re-open or resubmit. Otherwise continue: fill "
                                    "remaining fields, upload documents, and submit. If "
                                    "blocked, state the exact reason in one short "
                                    "paragraph. No page snapshots. End with the "
                                    "mandatory 'OUTCOME: <x>' line."
                                ),
                            })
                            continue
                        log.line(f"[agent] STOP after {turn} turns: exhausted "
                                 f"nudges without a valid OUTCOME line "
                                 f"(finish_reason={finish_reason})")
                        _record_trajectory(trajectory_id, "final", {
                            "rc": 1,
                            "outcome": "incomplete",
                            "turn": turn,
                            "reason": "missing_outcome",
                        })
                        return 1, content
                    log.line(f"[agent] DONE after {turn} turns")
                    _record_trajectory(trajectory_id, "final", {
                        "rc": 0,
                        "outcome": _extract_outcome(content),
                        "turn": turn,
                    })
                    return 0, content

                # Detect degenerate repeated-action loops: exact repeats (e.g.
                # ArrowDown x30) and short oscillations (e.g. click a button /
                # Escape the dialog it opened / click it again / Escape again)
                # that never repeat the same single action back-to-back but
                # still make no progress. See _trailing_cycle_repeats.
                sig = tuple((tc.function.name, tc.function.arguments) for tc in tool_calls)
                sig_history.append(sig)
                del sig_history[:-24]  # keep a short trailing window (period=3 needs >=6*3)
                repeats = max(
                    (_trailing_cycle_repeats(sig_history, period) for period in (1, 2, 3)),
                    default=0,
                )
                if repeats == 0:
                    repeat_nudged = False
                if repeats >= 4:
                    log.line(f"[agent] ABORT: action cycle repeated {repeats + 1}x with no progress")
                    _record_trajectory(trajectory_id, "final", {
                        "rc": 1,
                        "outcome": "incomplete",
                        "turn": turn,
                        "reason": "repeated_action_cycle",
                        "repeats": repeats + 1,
                    })
                    return 1, "Repeated the same action (or short cycle of actions) without progress."

                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    # Keep urls/refs intact; only clamp obviously long free text so
                    # the log doesn't mislead (a 60-char clamp made full URLs look
                    # truncated, masking real failures during debugging).
                    short = {k: (v if k in ("url", "ref", "element")
                                 else str(v)[:200]) for k, v in args.items()}
                    log.line(f"[agent] turn {turn} call {name} {short}")
                    _record_trajectory(trajectory_id, "tool_call", {
                        "turn": turn,
                        "tool": name,
                        "args": short,
                    })
                    if name == "lookup_credential":
                        # Handled locally; the password is returned to the model
                        # but never written to the transcript.
                        site = str(args.get("site", ""))
                        cred = credentials.lookup(site)
                        if cred:
                            text = (
                                f"username/email: {cred.get('username','')}\n"
                                f"password: {cred.get('password','')}"
                            )
                            log.line(f"[agent]   -> credential found for {site!r} (redacted)")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": True,
                                "summary": f"credential found for {site!r}",
                            })
                        else:
                            text = (
                                f"No stored credential for {site!r}. Stored sites: "
                                f"{', '.join(credentials.available_domains()) or '(none)'}."
                            )
                            log.line(f"[agent]   -> no credential for {site!r}")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": False,
                                "summary": f"no credential for {site!r}",
                            })
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": text,
                        })
                        continue
                    if name == "dom_scan":
                        # Local fallback, not routed through the Playwright MCP:
                        # see DOM_SCAN_TOOL for when this is appropriate. Ask
                        # the MCP which tab is current so we report on the
                        # same one the model has been looking at (see
                        # _current_tab_url). Caught broadly like the generic
                        # MCP call below -- a CDP/connection hiccup here must
                        # not kill the whole run.
                        try:
                            current_url = await _current_tab_url(session)
                            text = await browser_dom_tools.dom_scan(cdp_url, current_url=current_url)
                            log.line(f"[agent]   -> dom_scan ({len(text)} chars)")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": True,
                                "chars": len(text),
                            })
                        except Exception as e:
                            text = f"### Tool error\n{type(e).__name__}: {e}"
                            log.line(f"[agent]   -> dom_scan error: {e}")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": False,
                                "error": f"{type(e).__name__}: {e}",
                            })
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": _clamp_tool_result(text),
                        })
                        continue
                    if name == "click_by_text":
                        click_text = str(args.get("text", ""))
                        try:
                            current_url = await _current_tab_url(session)
                            text = await browser_dom_tools.click_by_text(
                                cdp_url, click_text, current_url=current_url)
                            log.line(f"[agent]   -> click_by_text {click_text!r}")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": True,
                                "summary": click_text,
                            })
                        except Exception as e:
                            text = f"### Tool error\n{type(e).__name__}: {e}"
                            log.line(f"[agent]   -> click_by_text error: {e}")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": False,
                                "error": f"{type(e).__name__}: {e}",
                            })
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": _clamp_tool_result(text),
                        })
                        continue
                    if name == "fill_by_label":
                        field_label = str(args.get("label", ""))
                        value = str(args.get("value", ""))
                        try:
                            current_url = await _current_tab_url(session)
                            text = await browser_dom_tools.fill_by_label(
                                cdp_url, field_label, value, current_url=current_url)
                            log.line(f"[agent]   -> fill_by_label {field_label!r}")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": True,
                                "summary": field_label,
                            })
                        except Exception as e:
                            text = f"### Tool error\n{type(e).__name__}: {e}"
                            log.line(f"[agent]   -> fill_by_label error: {e}")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": False,
                                "error": f"{type(e).__name__}: {e}",
                            })
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": _clamp_tool_result(text),
                        })
                        continue
                    if name == "select_option_by_label":
                        field_label = str(args.get("label", ""))
                        option = str(args.get("option", ""))
                        try:
                            current_url = await _current_tab_url(session)
                            text = await browser_dom_tools.select_option_by_label(
                                cdp_url, field_label, option, current_url=current_url)
                            log.line(f"[agent]   -> select_option_by_label {field_label!r} -> {option!r}")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": True,
                                "summary": f"{field_label} -> {option}",
                            })
                        except Exception as e:
                            text = f"### Tool error\n{type(e).__name__}: {e}"
                            log.line(f"[agent]   -> select_option_by_label error: {e}")
                            _record_trajectory(trajectory_id, "tool_result", {
                                "turn": turn,
                                "tool": name,
                                "ok": False,
                                "error": f"{type(e).__name__}: {e}",
                            })
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": _clamp_tool_result(text),
                        })
                        continue
                    if name == "browser_snapshot":
                        snapshot_calls += 1
                    try:
                        result = await session.call_tool(name, args)
                        text = _result_text(result)
                        _record_trajectory(trajectory_id, "tool_result", {
                            "turn": turn,
                            "tool": name,
                            "ok": True,
                            "chars": len(text),
                        })
                        # Deterministic cookie-banner sweep right after every
                        # navigation -- consent overlays intercept all clicks
                        # and otherwise cost LLM turns to clear (one run burned
                        # its final turns on exactly this). Free and fail-open.
                        if AUTO_COOKIE and name == "browser_navigate":
                            try:
                                note = await browser_dom_tools.dismiss_cookie_banner(
                                    cdp_url,
                                    current_url=await _current_tab_url(session))
                                if note:
                                    log.line(f"[agent]   -> auto {note}")
                                    text += f"\n[auto] {note}"
                            except Exception as e:  # noqa: BLE001
                                log.line(f"[agent]   -> cookie sweep error: {e}")
                    except Exception as e:  # surface tool errors to the model
                        text = f"### Tool error\n{type(e).__name__}: {e}"
                        log.line(f"[agent]   -> error: {e}")
                        _record_trajectory(trajectory_id, "tool_result", {
                            "turn": turn,
                            "tool": name,
                            "ok": False,
                            "error": f"{type(e).__name__}: {e}",
                        })
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id, "content": _clamp_tool_result(text),
                    })

                # Drop page dumps the model has already moved past -- see
                # _prune_stale_page_dumps for the measured cost of keeping them.
                pruned = _prune_stale_page_dumps(messages)
                if pruned:
                    log.line(f"[agent] pruned {pruned} stale page dump(s) from context")
                    _record_trajectory(trajectory_id, "guard", {
                        "name": "prune_stale_page_dumps",
                        "turn": turn,
                        "count": pruned,
                    })

                # Cross-source duplicate check: once per turn, see where the
                # browser actually is now. An aggregator listing (e.g.
                # huurwoningen.nl) reaches its real external destination only
                # after clicking through in-page redirect dialogs -- there is
                # no cheap way to resolve that URL before opening the browser,
                # so this is the earliest point a duplicate submission (same
                # physical listing, already handled via a different entry
                # point -- e.g. the Stekkies-mail path recording a DIFFERENT
                # URL for the same property) can be caught. Verified missing
                # in production: Hof van Oslo submitted once via Stekkies
                # (recorded under rebogroep.nl) then a SECOND time from a
                # manual retest of this exact agent via huurwoningen.nl,
                # because nothing cross-checked the two paths' URLs against
                # each other before this existed.
                current_url = await _current_tab_url(session)
                if current_url and _is_payment_url(current_url):
                    # HARD MONEY GUARD (see _PAYMENT_HOST_MARKERS): the browser
                    # reached a payment processor. Stop NOW, before the model
                    # can act on the checkout — never enter payment details.
                    log.line(f"[agent] PAYMENT PAGE reached ({current_url!r}); "
                             "aborting to avoid any real payment")
                    _record_trajectory(trajectory_id, "final", {
                        "rc": 0,
                        "outcome": "payment_required",
                        "turn": turn,
                        "url": current_url,
                    })
                    return 0, (
                        f"Reached a payment/checkout page ({current_url}). This "
                        "site requires paying to apply/register; I stopped "
                        "without paying, as instructed.\nOUTCOME: payment_required"
                    )
                if current_url:
                    current_canon = poller_dedup.canonical_url(current_url)
                    if current_canon != source_canon:
                        if resolved is not None:
                            resolved["url"] = current_url
                        if current_canon in known_urls:
                            log.line(
                                f"[agent] DUPLICATE: current destination {current_url!r} "
                                "already has a recorded submission from a different "
                                "source/entry point -- stopping without resubmitting."
                            )
                            _record_trajectory(trajectory_id, "final", {
                                "rc": 0,
                                "outcome": "already_applied",
                                "turn": turn,
                                "url": current_url,
                                "reason": "cross_source_duplicate",
                            })
                            return 0, (
                                f"This listing's actual destination ({current_url}) already "
                                "has a recorded submission reached via a different source/"
                                "entry point for the same property. Not resubmitting.\n"
                                "OUTCOME: already_applied"
                            )

                # After answering the tool calls, prod the model if it's looping.
                if repeats >= 2 and not repeat_nudged:
                    repeat_nudged = True
                    log.line("[agent] repeat-guard nudge")
                    _record_trajectory(trajectory_id, "guard", {
                        "name": "repeat_action_nudge",
                        "turn": turn,
                        "repeats": repeats + 1,
                    })
                    messages.append({
                        "role": "user",
                        "content": (
                            "You have repeated the same action several times with no "
                            "progress. STOP repeating it. Take ONE fresh browser_snapshot, "
                            "then either do something clearly different, or if you cannot "
                            "proceed, stop and report the exact status in one short "
                            "paragraph (no page snapshots)."
                        ),
                    })
                if not snapshot_nudged and _should_nudge_snapshot_overuse(snapshot_calls, turn):
                    snapshot_nudged = True
                    log.line(f"[agent] snapshot-overuse nudge "
                             f"({snapshot_calls} snapshots in {turn} turns)")
                    _record_trajectory(trajectory_id, "guard", {
                        "name": "snapshot_overuse_nudge",
                        "turn": turn,
                        "snapshot_calls": snapshot_calls,
                    })
                    messages.append({
                        "role": "user",
                        "content": (
                            f"You've taken {snapshot_calls} snapshots in {turn} turns. "
                            "A click/type result usually already tells you whether it "
                            "worked -- you rarely need a fresh browser_snapshot right "
                            "after every action. Only re-snapshot when the page has "
                            "genuinely changed (new URL, a dialog opened/closed) and you "
                            "need new refs. If a dialog or overlay seems to have opened "
                            "but browser_snapshot doesn't show it, use dom_scan (raw DOM, "
                            "not the accessibility tree) instead of re-snapshotting "
                            "repeatedly."
                        ),
                    })

            log.line(f"[agent] STOP: hit turn budget={budget} "
                     f"(max_turns={max_turns}, grace={grace_granted})")
            _record_trajectory(trajectory_id, "final", {
                "rc": 1,
                "outcome": "incomplete",
                "turn": turn,
                "reason": "turn_budget",
                "budget": budget,
            })
            return 1, "Hit the turn budget before reaching a conclusion."


# How long after the wall-clock timeout the teardown may take before the
# watchdog assumes the MCP subprocess is wedged and kills it. asyncio.wait_for
# only CANCELS the task; the cancellation still has to unwind stdio_client's
# __aexit__, which waits on the npx/node MCP process -- if that process
# ignores its closed stdin, asyncio.run() blocks forever with the browser
# flock still held. That is the one failure mode the timeout cannot cover
# (03-07-2026: the lock stayed held for 9+ hours, starving eight consecutive
# mail-triggered applies).
TEARDOWN_GRACE_SECONDS = int(os.environ.get("APPLY_TEARDOWN_GRACE_SECONDS", "120"))

_CHILD_MARKERS = (b"playwright", b"mcp", b"node", b"npx")


def _descendant_pids(root_pid: int) -> list[int]:
    """All live descendant pids of root_pid, via /proc (no psutil dependency)."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = (Path("/proc") / entry / "stat").read_text()
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out: list[int] = []
    queue = [root_pid]
    while queue:
        for child in children.get(queue.pop(), []):
            out.append(child)
            queue.append(child)
    return out


def _kill_wedged_children(root_pid: int) -> list[int]:
    """SIGKILL descendant processes that look like the MCP/Playwright stack
    (node/npx). Killing them closes the stdio pipes a hung teardown is
    blocked on, letting asyncio.run() finally return and release the browser
    flock."""
    killed: list[int] = []
    for pid in _descendant_pids(root_pid):
        try:
            cmdline = (Path("/proc") / str(pid) / "cmdline").read_bytes().lower()
        except OSError:
            continue
        if any(m in cmdline for m in _CHILD_MARKERS):
            try:
                os.kill(pid, signal.SIGKILL)
                killed.append(pid)
            except OSError:
                pass
    return killed


class Logger:
    """Tee log lines to stdout (live) and a file."""
    def __init__(self, path: Path):
        self.path = path
        self.fh = open(path, "w", encoding="utf-8")

    def line(self, s: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        out = f"{stamp} {s}"
        print(out, flush=True)
        self.fh.write(out + "\n")
        self.fh.flush()

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass


def run_agent(prompt: str, model: str, max_turns: int, cdp_url: str, log_path: Path,
              timeout_seconds: int = 900, source_url: str = "",
              yield_check=None) -> AgentResult:
    log = Logger(log_path)
    resolved: dict = {}

    async def _with_timeout() -> tuple[int, str]:
        try:
            return await asyncio.wait_for(
                _run(prompt, model, max_turns, cdp_url, log, source_url=source_url,
                     resolved=resolved, yield_check=yield_check,
                     trajectory_id=log_path.stem),
                timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log.line(f"[agent] TIMEOUT after {timeout_seconds}s")
            return 124, "Timed out before reaching a conclusion."

    # Teardown watchdog: see TEARDOWN_GRACE_SECONDS. Fires only when the run
    # has blown past its wall-clock timeout AND the grace on top of it -- at
    # that point the MCP subprocess is wedged and holding everything hostage.
    def _watchdog_fire() -> None:
        killed = _kill_wedged_children(os.getpid())
        try:
            log.line(f"[agent] WATCHDOG: teardown wedged "
                     f"{TEARDOWN_GRACE_SECONDS}s past timeout; killed MCP "
                     f"child pids {killed or '(none found)'}")
        except Exception:  # noqa: BLE001 - the kill is what matters
            pass

    watchdog = threading.Timer(timeout_seconds + TEARDOWN_GRACE_SECONDS,
                               _watchdog_fire)
    watchdog.daemon = True
    watchdog.start()
    try:
        rc, final_text = asyncio.run(_with_timeout())
    finally:
        watchdog.cancel()
        log.close()

    outcome = _parse_outcome(final_text, rc)
    # Trim any OUTCOME: line out of the human summary.
    summary = re.sub(r"\n?OUTCOME:\s*[a-z_]+\s*$", "", (final_text or "").strip(),
                     flags=re.IGNORECASE).strip()
    return AgentResult(rc=rc, outcome=outcome, summary=summary[:500],
                       resolved_url=resolved.get("url", ""))
