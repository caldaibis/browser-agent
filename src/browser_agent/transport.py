"""MCP/OpenAI plumbing for the agent loop: server params, tool-schema
conversion, tool-result flattening, current-tab detection, the transcript
Logger, and the wedged-MCP-teardown SIGKILL watchdog."""
from __future__ import annotations

import os
import re
import signal
from datetime import datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters

from ..agent_tools import BLOCKED_TOOLS
from ..settings import settings


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


async def _current_tab_url(session: ClientSession) -> str | None:
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


# How long after the wall-clock timeout the teardown may take before the
# watchdog assumes the MCP subprocess is wedged and kills it. asyncio.wait_for
# only CANCELS the task; the cancellation still has to unwind stdio_client's
# __aexit__, which waits on the npx/node MCP process -- if that process
# ignores its closed stdin, asyncio.run() blocks forever with the browser
# flock still held. That is the one failure mode the timeout cannot cover
# (03-07-2026: the lock stayed held for 9+ hours, starving eight consecutive
# mail-triggered applies).
TEARDOWN_GRACE_SECONDS = settings().apply_teardown_grace_seconds

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
        # Long-lived handle owned by this object; released in close().
        self.fh = open(path, "w", encoding="utf-8")  # noqa: SIM115

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
