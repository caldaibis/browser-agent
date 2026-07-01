"""Minimal browser agent loop — our lightweight replacement for Hermes.

Connects to the Playwright MCP server (stdio) attached to our shared CDP browser,
exposes its tools to an OpenRouter-hosted LLM, and runs a tool-calling loop until
the model produces a final text answer or the turn budget is hit.

What this gives us over Hermes: full control, our logging, no 1.2 GB harness —
just the agentic loop + MCP client. The Playwright MCP (the genuinely valuable
piece: snapshot/click/fill_form/file_upload) is unchanged.

Env:
  OPENROUTER_API_KEY   required — your OpenRouter key.

Public API:
  run_agent(...) -> AgentResult  (rc, outcome, summary)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import credentials

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Reasoning control. glm-5.2 is a reasoning model and, left alone, burns hundreds
# of hidden reasoning tokens before emitting content/tool_calls — over a large
# page snapshot that can run long enough to hit the completion cap mid-reasoning
# (finish_reason="length", empty content, no tool_calls) and look like a stall.
# Filling a rental form is not rocket science, so we DISABLE reasoning by default.
# Empirically glm ignores fine-grained reasoning caps (max_tokens/effort barely
# move it) but honours {"enabled": False} → 0 reasoning tokens, and still answers
# correctly. Override via APPLY_REASONING_EFFORT = off | minimal | low | medium |
# high (anything but "off" re-enables reasoning at that effort).
REASONING_EFFORT = os.environ.get("APPLY_REASONING_EFFORT", "off").lower()
REASONING = ({"enabled": False} if REASONING_EFFORT in ("off", "none", "false", "0")
             else {"effort": REASONING_EFFORT})

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
            "string if no credential is stored for that site."
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

# Playwright MCP tools we never want the model to use (raw JS = token bleed).
BLOCKED_TOOLS = {"browser_evaluate", "browser_run_code_unsafe"}

# Outcomes the model may declare via a final "OUTCOME: <x>" line.
VALID_OUTCOMES = {
    "submitted", "already_applied", "not_available", "not_eligible",
    "login_required", "blocked",
}


@dataclass
class AgentResult:
    rc: int            # 0 ok, 1 incomplete/loop, 2 setup error, 124 timeout
    outcome: str       # one of VALID_OUTCOMES, or incomplete/timeout/error/unknown
    summary: str       # the model's final one-paragraph status

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


async def _run(prompt: str, model: str, max_turns: int, cdp_url: str, log: "Logger") -> tuple[int, str]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        log.line("[agent] ERROR: OPENROUTER_API_KEY not set")
        return 2, "OPENROUTER_API_KEY not set."

    client = AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)

    async with stdio_client(_mcp_params(cdp_url)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = _to_openai_tools(mcp_tools) + [CREDENTIAL_TOOL]
            log.line(f"[agent] model={model} tools={len(tools)} cdp={cdp_url}")

            messages: list[dict] = [{"role": "user", "content": prompt}]
            nudges_left = 2  # if the model stops early without finishing, prod it
            trunc_retries_left = 4  # tolerate transient truncated/empty completions
            last_sig = None  # detect degenerate repeated-action loops
            repeats = 0
            for turn in range(1, max_turns + 1):
                resp = await client.chat.completions.create(
                    model=model, messages=messages, tools=tools, tool_choice="auto",
                    max_tokens=MAX_TOKENS, extra_body={"reasoning": REASONING},
                )
                choice = resp.choices[0]
                msg = choice.message
                tool_calls = msg.tool_calls or []
                finish_reason = choice.finish_reason

                # Reasoning *text* is hidden, but the *count* is reported. Log it
                # so we can tell "thought too much" (high reasoning_tokens, near the
                # cap) from "cap too low" (finish_reason=length at modest counts).
                usage = getattr(resp, "usage", None)
                ctd = getattr(usage, "completion_tokens_details", None)
                reasoning_tok = getattr(ctd, "reasoning_tokens", None)
                completion_tok = getattr(usage, "completion_tokens", None)
                log.line(f"[agent] turn {turn} finish={finish_reason} "
                         f"completion_tokens={completion_tok} "
                         f"reasoning_tokens={reasoning_tok} (cap={MAX_TOKENS})")

                # A turn cut off mid-reasoning comes back as finish_reason="length"
                # with empty content and no tool_calls — a truncation glitch. We've
                # also seen finish_reason="stop" with empty content and no
                # tool_calls at the same completion-token cost as the model's own
                # successful bare-arg tool calls elsewhere in the same transcript —
                # almost certainly a tool call OpenRouter failed to surface, not a
                # deliberate conclusion (the prompt requires a non-empty OUTCOME
                # line, which empty content can never satisfy). Treat both as the
                # same transport glitch: retry (don't spend a nudge / declare done).
                if not tool_calls and not (msg.content or "").strip() \
                        and finish_reason in ("length", "stop") and trunc_retries_left > 0:
                    trunc_retries_left -= 1
                    log.line(f"[agent] turn {turn} truncated/dropped "
                             f"(finish_reason={finish_reason}, empty); retrying "
                             f"({trunc_retries_left} left)")
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
                if tool_calls:
                    assistant_entry["tool_calls"] = [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in tool_calls
                    ]
                messages.append(assistant_entry)

                if msg.content:
                    log.line(f"[agent] turn {turn} say: {msg.content.strip()[:400]}")

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
                        return 1, content
                    log.line(f"[agent] DONE after {turn} turns")
                    return 0, content

                # Detect degenerate repeated-action loops (e.g. ArrowDown x30).
                sig = tuple((tc.function.name, tc.function.arguments) for tc in tool_calls)
                repeats = repeats + 1 if sig == last_sig else 0
                last_sig = sig
                if repeats >= 4:
                    log.line(f"[agent] ABORT: same action repeated {repeats + 1}x with no progress")
                    return 1, "Repeated the same action without progress."

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
                        else:
                            text = (
                                f"No stored credential for {site!r}. Stored sites: "
                                f"{', '.join(credentials.available_domains()) or '(none)'}."
                            )
                            log.line(f"[agent]   -> no credential for {site!r}")
                        messages.append({
                            "role": "tool", "tool_call_id": tc.id, "content": text,
                        })
                        continue
                    try:
                        result = await session.call_tool(name, args)
                        text = _result_text(result)
                    except Exception as e:  # surface tool errors to the model
                        text = f"### Tool error\n{type(e).__name__}: {e}"
                        log.line(f"[agent]   -> error: {e}")
                    messages.append({
                        "role": "tool", "tool_call_id": tc.id, "content": text[:20000],
                    })

                # After answering the tool calls, prod the model if it's looping.
                if repeats == 2:
                    log.line("[agent] repeat-guard nudge")
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

            log.line(f"[agent] STOP: hit max_turns={max_turns}")
            return 1, "Hit the turn budget before reaching a conclusion."


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
              timeout_seconds: int = 900) -> AgentResult:
    log = Logger(log_path)

    async def _with_timeout() -> tuple[int, str]:
        try:
            return await asyncio.wait_for(
                _run(prompt, model, max_turns, cdp_url, log), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log.line(f"[agent] TIMEOUT after {timeout_seconds}s")
            return 124, "Timed out before reaching a conclusion."

    try:
        rc, final_text = asyncio.run(_with_timeout())
    finally:
        log.close()

    outcome = _parse_outcome(final_text, rc)
    # Trim any OUTCOME: line out of the human summary.
    summary = re.sub(r"\n?OUTCOME:\s*[a-z_]+\s*$", "", (final_text or "").strip(),
                     flags=re.IGNORECASE).strip()
    return AgentResult(rc=rc, outcome=outcome, summary=summary[:500])
