"""Killable subprocess entry point for a real Playwright MCP handshake."""
from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from .browser_agent.transport import _mcp_params


async def _check(cdp_url: str) -> int:
    async with stdio_client(_mcp_params(cdp_url)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"Playwright MCP initialize succeeded; tools={len(tools)}")
    return 0


def main() -> int:
    cdp_url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9222"
    return asyncio.run(_check(cdp_url))


if __name__ == "__main__":
    raise SystemExit(main())
