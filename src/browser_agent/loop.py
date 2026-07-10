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
import threading
from pathlib import Path
from typing import Any

from openai import AsyncOpenAI, APIStatusError
from mcp import ClientSession
from mcp.client.stdio import stdio_client

from .. import browser_dom_tools, credentials
from ..agent_tools import (
    CLICK_BY_TEXT_TOOL,
    CREDENTIAL_TOOL,
    DOM_SCAN_TOOL,
    FILL_BY_LABEL_TOOL,
    SELECT_OPTION_BY_LABEL_TOOL,
)
from .guards import (
    GRACE_TURNS,
    _clamp_tool_result,
    _is_payment_url,
    _prune_stale_page_dumps,
    _recent_form_activity,
    _should_nudge_snapshot_overuse,
    _trailing_cycle_repeats,
)
from .result import NO_CREDIT_RC, AgentResult, _extract_outcome, _parse_outcome
from .transport import (
    TEARDOWN_GRACE_SECONDS,
    Logger,
    _current_tab_url,
    _kill_wedged_children,
    _mcp_params,
    _result_text,
    _to_openai_tools,
)
from ..poller import dedup as poller_dedup
from ..self_improvement_harness import record_trajectory_event
from ..settings import settings

DEEPSEEK_BASE_URL = settings().deepseek_base_url

# Reasoning control. DeepSeek thinking mode can burn hidden reasoning
# tokens before emitting content/tool_calls. Over a large page snapshot that can
# hit the completion cap mid-reasoning (finish_reason="length", empty content,
# no tool_calls) and look like a stall. Filling a rental form is not rocket
# science, so we DISABLE reasoning by default. Override via
# APPLY_REASONING_EFFORT = off | low | medium | high | max (anything but
# "off" re-enables reasoning at that effort).
REASONING_EFFORT = settings().apply_reasoning_effort
THINKING = (
    {"type": "disabled"}
    if REASONING_EFFORT in ("off", "none", "false", "0")
    else {"type": "enabled"}
)

# Completion cap per turn. With reasoning off the visible output (a tool call or a
# short status) is small, so this is just generous headroom against truncation.
MAX_TOKENS = settings().apply_max_tokens

# Deterministic cookie-banner dismissal after every navigation (free — no LLM
# turn). Cookie/consent overlays intercept clicks and eat turns: one observed
# run burned its final turns clicking a consent modal away field by field.
AUTO_COOKIE = settings().apply_auto_cookie

def _record_trajectory(run_id: str, event: str, payload: dict | None = None) -> None:
    if run_id:
        record_trajectory_event(run_id, event, payload or {})


async def _run(prompt: str, model: str, max_turns: int, cdp_url: str, log: Logger,
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
    api_key = settings().deepseek_api_key
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
                request: dict[str, Any] = {
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
        except TimeoutError:
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
