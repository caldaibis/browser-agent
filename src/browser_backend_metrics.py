"""Deterministic browser-backend contract metrics.

This compares the pinned Playwright rollback contract with the production
agent-browser contract without invoking an LLM or navigating the network.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from .agent_tools import (
    CLICK_BY_TEXT_TOOL,
    CREDENTIAL_TOOL,
    DOM_SCAN_TOOL,
    FILL_BY_LABEL_TOOL,
    LOGIN_WITH_CREDENTIAL_TOOL,
    SELECT_OPTION_BY_LABEL_TOOL,
)
from .browser_agent import transport


SHARED_LOCAL_TOOLS = [
    DOM_SCAN_TOOL,
    CLICK_BY_TEXT_TOOL,
    FILL_BY_LABEL_TOOL,
    SELECT_OPTION_BY_LABEL_TOOL,
]

CAPABILITY_TOOLS = {
    "navigation": {"browser_navigate"},
    "accessibility_snapshot": {"browser_snapshot"},
    "bulk_form_fill": {"browser_fill_form"},
    "document_upload": {"browser_file_upload"},
    "native_select": {"browser_select_option"},
    "tab_control": {"browser_tabs"},
    "dialog_control": {"browser_handle_dialog"},
    "page_error_inspection": {"browser_errors"},
    "semantic_locator": {"browser_find"},
    "snapshot_diff": {"browser_snapshot_diff"},
    "rendered_page_read": {"browser_read_page"},
    "secure_credential_login": {"login_with_credential"},
}

RISK_TOOL_NAMES = {
    "browser_close": "shared_browser_lifecycle",
    "browser_evaluate": "arbitrary_javascript",
    "browser_run_code": "arbitrary_javascript",
    "browser_run_code_unsafe": "arbitrary_javascript",
    "agent_browser_close": "shared_browser_lifecycle",
    "agent_browser_eval": "arbitrary_javascript",
}


def _canonical_bytes(tools: list[dict]) -> int:
    return len(json.dumps(
        tools, ensure_ascii=True, separators=(",", ":"), sort_keys=True,
    ).encode())


def _tool_names(tools: list[dict]) -> set[str]:
    return {str(tool["function"]["name"]) for tool in tools}


def summarize_backend(backend: str, upstream_tools: list, version: str) -> dict:
    browser_tools = transport._to_openai_tools(upstream_tools, backend)
    credential_tool = (
        LOGIN_WITH_CREDENTIAL_TOOL if backend == "agent_browser" else CREDENTIAL_TOOL
    )
    apply_tools = browser_tools + [credential_tool, *SHARED_LOCAL_TOOLS]
    names = _tool_names(apply_tools)
    risks = {
        name: RISK_TOOL_NAMES[name]
        for name in sorted(names & RISK_TOOL_NAMES.keys())
    }
    capabilities = sorted(
        capability
        for capability, required in CAPABILITY_TOOLS.items()
        if required <= names
    )
    return {
        "runtime_version": version,
        "upstream_mcp_tools": len(upstream_tools),
        "browser_tools_exposed_to_model": len(browser_tools),
        "total_apply_tools_exposed_to_model": len(apply_tools),
        "tool_contract_bytes_per_model_request": _canonical_bytes(apply_tools),
        "risk_tools_exposed_to_model": risks,
        "capabilities": capabilities,
        "capability_count": len(capabilities),
        "password_returned_to_model": backend != "agent_browser",
        "normalized_browser_contract": backend == "agent_browser",
    }


async def _discover(backend: str) -> tuple[list, str]:
    params = transport._mcp_params(
        "http://127.0.0.1:1", backend, namespace=f"metrics-{backend}")
    with tempfile.TemporaryDirectory() as socket_dir:
        if backend == "agent_browser":
            params.env = {
                **(params.env or {}),
                "AGENT_BROWSER_SOCKET_DIR": socket_dir,
            }
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                info = await session.initialize()
                tools = await transport._list_mcp_tools(session)
                version = str(getattr(getattr(info, "serverInfo", None), "version", "?"))
                return tools, version


async def build_report() -> dict:
    playwright_discovery, agent_discovery = await asyncio.gather(
        _discover("playwright"),
        _discover("agent_browser"),
    )
    playwright = summarize_backend("playwright", *playwright_discovery)
    agent_browser = summarize_backend("agent_browser", *agent_discovery)
    before_caps = set(playwright["capabilities"])
    after_caps = set(agent_browser["capabilities"])
    before_risks = set(playwright["risk_tools_exposed_to_model"])
    after_risks = set(agent_browser["risk_tools_exposed_to_model"])
    before_bytes = playwright["tool_contract_bytes_per_model_request"]
    after_bytes = agent_browser["tool_contract_bytes_per_model_request"]
    gates = {
        "tool_contract_is_smaller": after_bytes < before_bytes,
        "capabilities_are_a_strict_superset": after_caps > before_caps,
        "no_new_risk_tools": after_risks <= before_risks,
        "password_no_longer_returns_to_model": (
            playwright["password_returned_to_model"]
            and not agent_browser["password_returned_to_model"]
        ),
    }
    return {
        "schema": "browser-backend-contract-metrics-v1",
        "measurement": (
            "Pinned MCP discovery plus canonical JSON serialization; no LLM, "
            "website, timing, or production data."
        ),
        "baseline": "playwright",
        "candidate": "agent_browser",
        "backends": {
            "playwright": playwright,
            "agent_browser": agent_browser,
        },
        "comparison": {
            "tool_contract_bytes_delta": after_bytes - before_bytes,
            "tool_contract_bytes_change_percent": round(
                ((after_bytes - before_bytes) / before_bytes) * 100, 2),
            "capabilities_added": sorted(after_caps - before_caps),
            "capabilities_removed": sorted(before_caps - after_caps),
            "risk_tools_removed": sorted(before_risks - after_risks),
            "risk_tools_added": sorted(after_risks - before_risks),
            "password_returned_to_model_before": playwright["password_returned_to_model"],
            "password_returned_to_model_after": agent_browser["password_returned_to_model"],
        },
        "gates": {**gates, "all_passed": all(gates.values())},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Also write the JSON report here.")
    args = parser.parse_args()
    report = asyncio.run(build_report())
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")
    if not report["gates"]["all_passed"]:
        raise SystemExit("browser backend improvement gates failed")


if __name__ == "__main__":
    main()
