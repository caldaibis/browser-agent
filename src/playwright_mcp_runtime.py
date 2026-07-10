"""Pinned Playwright MCP runtime contract shared by apply/deploy/health."""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PLAYWRIGHT_MCP_VERSION = (
    Path(__file__).resolve().parent.parent / "deploy" / "playwright-mcp.version"
).read_text(encoding="utf-8").strip()
PLAYWRIGHT_MCP_PACKAGE = f"@playwright/mcp@{PLAYWRIGHT_MCP_VERSION}"
MIN_NODE_MAJOR = 20


def node_major(version_text: str) -> int | None:
    match = re.search(r"v?(\d+)(?:\.\d+){0,2}", version_text or "")
    return int(match.group(1)) if match else None


def runtime_check(*, timeout: int = 20) -> tuple[bool, str]:
    """Exercise the pinned CLI startup path; catches engine drift before apply."""
    try:
        node = subprocess.run(
            ["node", "--version"], capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"node unavailable: {exc}"
    major = node_major(node.stdout or node.stderr)
    if node.returncode != 0 or major is None or major < MIN_NODE_MAJOR:
        return False, (
            f"Node {node.stdout.strip() or node.stderr.strip() or '?'} is too old; "
            f"Playwright MCP requires Node {MIN_NODE_MAJOR}+"
        )
    try:
        probe = subprocess.run(
            ["npx", "--yes", PLAYWRIGHT_MCP_PACKAGE, "--help"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"pinned Playwright MCP startup failed: {exc}"
    output = (probe.stdout + probe.stderr).strip()
    if probe.returncode != 0:
        return False, f"pinned Playwright MCP startup rc={probe.returncode}: {output[-1000:]}"
    return True, f"Node {major}; {PLAYWRIGHT_MCP_PACKAGE} starts successfully"


def initialize_check(cdp_url: str, *, timeout: int = 30) -> tuple[bool, str]:
    """Run a real MCP initialize/list-tools handshake in a killable child."""
    try:
        probe = subprocess.run(
            [sys.executable, "-m", "src.playwright_mcp_smoke", cdp_url],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"Playwright MCP initialize exceeded {timeout}s"
    output = (probe.stdout + probe.stderr).strip()
    if probe.returncode != 0:
        return False, f"Playwright MCP initialize rc={probe.returncode}: {output[-1500:]}"
    return True, output[-1000:] or "Playwright MCP initialize succeeded"
