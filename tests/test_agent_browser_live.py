from __future__ import annotations

import asyncio
import os
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from mcp import ClientSession
from mcp.client.stdio import stdio_client
from playwright.async_api import async_playwright

from src.browser_agent import transport


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "browser_agent"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _agent_browser_available() -> bool:
    if os.environ.get("RUN_AGENT_BROWSER_LIVE") != "1":
        return False
    command = shutil.which("agent-browser")
    if not command:
        return False
    expected = (Path(__file__).parents[1] / "deploy" / "agent-browser.version").read_text().strip()
    result = subprocess.run(
        [command, "--version"], check=False, capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip().endswith(expected)


@unittest.skipUnless(_agent_browser_available(), "pinned agent-browser is not installed")
class TestAgentBrowserLive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURE_DIR))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.http_thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.http_thread.start()

        self.cdp_port = _free_port()
        playwright = await async_playwright().start()
        executable = playwright.chromium.executable_path
        await playwright.stop()
        self.profile_tmp = tempfile.TemporaryDirectory()
        self.browser = await asyncio.create_subprocess_exec(
            executable, "--headless=new", "--no-sandbox", "--disable-gpu",
            f"--remote-debugging-port={self.cdp_port}",
            f"--user-data-dir={self.profile_tmp.name}", "about:blank",
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                if sock.connect_ex(("127.0.0.1", self.cdp_port)) == 0:
                    break
            await asyncio.sleep(0.05)
        else:
            self.fail("disposable Chromium did not expose its CDP port")
        self.namespace = f"stekkies-test-{os.getpid()}-{id(self)}"

    async def asyncTearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.browser.returncode is None:
            os.killpg(self.browser.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.browser.wait(), timeout=5)
            except TimeoutError:
                os.killpg(self.browser.pid, signal.SIGKILL)
                await self.browser.wait()
        self.profile_tmp.cleanup()

    async def test_normalized_production_surface_against_real_chromium(self):
        url = (
            f"http://127.0.0.1:{self.httpd.server_port}/"
            "agent_browser_regression.html"
        )

        async def call(name: str, args: dict) -> tuple[str, bool]:
            return await asyncio.wait_for(
                transport._call_browser_tool(
                    session, name, args),
                timeout=15,
            )

        async with stdio_client(transport._mcp_params(
                f"http://127.0.0.1:{self.cdp_port}",
                namespace=self.namespace)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await transport._list_mcp_tools(session)
                model_tools = transport._to_openai_tools(tools)
                names = {tool["function"]["name"] for tool in model_tools}
                self.assertIn("browser_file_upload", names)
                self.assertNotIn("agent_browser_eval", names)

                text, ok = await call("browser_navigate", {"url": url})
                self.assertTrue(ok, text)

                text, ok = await call("browser_snapshot",
                    {"interactive": False, "compact": False})
                self.assertTrue(ok, text)
                self.assertIn("Minimum gross income", text)
                self.assertIn("PAGE_CONTENT", text)

                text, ok = await call("browser_click", {"target": "#covered"})
                self.assertFalse(ok)
                self.assertIn("cover", text.lower())

                text, ok = await call("browser_find",
                    {"locator": "text", "value": "Accept cookies", "action": "click", "exact": True})
                self.assertTrue(ok, text)
                text, ok = await call("browser_find",
                    {"locator": "text", "value": "Request viewing", "action": "click", "exact": True})
                self.assertTrue(ok, text)

                text, ok = await call("browser_snapshot",
                    {"selector": "dialog[open]", "interactive": True})
                self.assertTrue(ok, text)
                self.assertIn("First name", text)
                self.assertNotIn("Minimum gross income", text)

                text, ok = await call("browser_fill_form", {"fields": [
                        {"name": "First name", "target": "dialog[open] input[type=text]", "type": "textbox", "value": "Ada"},
                        {"name": "Email", "target": "dialog[open] input[type=email]", "type": "textbox", "value": "ada@example.test"},
                    ]})
                self.assertTrue(ok, text)
                diff, ok = await call("browser_snapshot_diff",
                    {"selector": "dialog[open]"})
                self.assertTrue(ok, diff)
                self.assertIn("ada@example.test", diff.lower())

                with tempfile.TemporaryDirectory() as tmp:
                    document = Path(tmp) / "income.pdf"
                    document.write_bytes(b"fixture")
                    with patch.object(transport, "DOCS_DIR", Path(tmp)):
                        upload, ok = await call("browser_file_upload", {
                                "target": "dialog[open] input[type=file]",
                                "paths": [str(document)],
                            })
                    self.assertTrue(ok, upload)

                submitted, ok = await call("browser_click", {"target": "#submit"})
                self.assertTrue(ok, submitted)
                confirmation, ok = await call("browser_snapshot",
                    {"selector": "dialog[open]", "interactive": False, "compact": False})
                self.assertTrue(ok, confirmation)
                self.assertIn("Application submitted", confirmation)

                tabs, ok = await call("browser_tabs", {"action": "list"})
                self.assertTrue(ok, tabs)
                self.assertIn("t", tabs)
                self.assertEqual(
                    await transport._current_tab_url(session), url)

if __name__ == "__main__":
    unittest.main()
