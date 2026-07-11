"""Pinned agent-browser runtime checks used by deployment and healthcheck."""
from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from .settings import settings

AGENT_BROWSER_VERSION = (
    Path(__file__).resolve().parent.parent / "deploy" / "agent-browser.version"
).read_text(encoding="utf-8").strip()


def _argv() -> list[str]:
    configured = shlex.split(settings().agent_browser_command)
    if not configured:
        return []
    executable = shutil.which(configured[0]) or configured[0]
    return [executable, *configured[1:]]


def version_check(*, timeout: int = 5) -> tuple[bool, str]:
    """Verify the installed agent-browser binary matches this checkout."""
    argv = _argv()
    if not argv:
        return False, "agent-browser command is empty"
    try:
        result = subprocess.run(
            [*argv, "--version"], capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"agent-browser unavailable: {exc}"
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return False, f"agent-browser --version rc={result.returncode}: {output[-500:]}"
    if AGENT_BROWSER_VERSION not in output:
        return False, (
            f"agent-browser version mismatch: expected {AGENT_BROWSER_VERSION}, "
            f"got {output[-500:] or '(no output)'}"
        )
    return True, f"agent-browser {AGENT_BROWSER_VERSION}"


def startup_check(*, timeout: int = 20) -> tuple[bool, str]:
    """Verify the installed binary exposes the MCP subcommand."""
    ok, detail = version_check(timeout=min(timeout, 5))
    if not ok:
        return False, detail
    argv = _argv()
    try:
        probe = subprocess.run(
            [*argv, "mcp", "--help"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"agent-browser MCP startup failed: {exc}"
    output = (probe.stdout + probe.stderr).strip()
    if probe.returncode != 0:
        return False, f"agent-browser MCP startup rc={probe.returncode}: {output[-1000:]}"
    return True, f"{detail}; MCP subcommand available"
