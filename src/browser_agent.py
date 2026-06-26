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
  run_agent(prompt, model, max_turns, cdp_url, log_path) -> int  (0 = success)
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Playwright MCP tools we never want the model to use (raw JS = token bleed).
BLOCKED_TOOLS = {"browser_evaluate", "browser_run_code_unsafe"}


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


async def _run(prompt: str, model: str, max_turns: int, cdp_url: str, log: "Logger") -> int:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        log.line("[agent] ERROR: OPENROUTER_API_KEY not set")
        return 2

    client = AsyncOpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)

    async with stdio_client(_mcp_params(cdp_url)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = (await session.list_tools()).tools
            tools = _to_openai_tools(mcp_tools)
            log.line(f"[agent] model={model} tools={len(tools)} cdp={cdp_url}")

            messages: list[dict] = [{"role": "user", "content": prompt}]
            nudges_left = 2  # if the model stops early without finishing, prod it
            last_sig = None  # detect degenerate repeated-action loops
            repeats = 0
            for turn in range(1, max_turns + 1):
                resp = await client.chat.completions.create(
                    model=model, messages=messages, tools=tools, tool_choice="auto",
                )
                msg = resp.choices[0].message
                tool_calls = msg.tool_calls or []

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
                    # Guard against the model ending early by dumping a page
                    # snapshot or stalling before it actually submitted/uploaded.
                    looks_unfinished = (
                        not content
                        or content.startswith("- ")          # raw snapshot YAML
                        or "[ref=e" in content               # snapshot refs
                        or len(content) > 1500               # huge = page dump
                    )
                    if looks_unfinished and nudges_left > 0:
                        nudges_left -= 1
                        log.line(f"[agent] nudge (model stopped without a clear "
                                 f"conclusion; {nudges_left} left)")
                        messages.append({
                            "role": "user",
                            "content": (
                                "That was not a final answer. Continue the task: "
                                "upload every required document with "
                                "browser_file_upload and SUBMIT the application. "
                                "Only stop once you have submitted, or state the "
                                "exact blocking reason in one short paragraph "
                                "(no page snapshots)."
                            ),
                        })
                        continue
                    log.line(f"[agent] DONE after {turn} turns")
                    return 0

                # Detect degenerate repeated-action loops (e.g. ArrowDown x30).
                sig = tuple((tc.function.name, tc.function.arguments) for tc in tool_calls)
                repeats = repeats + 1 if sig == last_sig else 0
                last_sig = sig
                if repeats >= 4:
                    log.line(f"[agent] ABORT: same action repeated {repeats + 1}x with no progress")
                    return 1

                for tc in tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    short = {k: (str(v)[:60]) for k, v in args.items()}
                    log.line(f"[agent] turn {turn} call {name} {short}")
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
            return 1


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
              timeout_seconds: int = 900) -> int:
    log = Logger(log_path)

    async def _with_timeout():
        try:
            return await asyncio.wait_for(
                _run(prompt, model, max_turns, cdp_url, log), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            log.line(f"[agent] TIMEOUT after {timeout_seconds}s")
            return 124

    try:
        return asyncio.run(_with_timeout())
    finally:
        log.close()
