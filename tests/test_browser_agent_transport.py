from __future__ import annotations

import tempfile
import unittest
import shutil
import os
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mcp import ClientSession
from mcp.client.stdio import stdio_client

from src.browser_agent import transport


class _Result:
    def __init__(self, text: str = "ok", error: bool = False):
        self.content = [SimpleNamespace(text=text)]
        self.isError = error


class _Session:
    def __init__(self, errors: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.errors = errors or set()

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if name == "agent_browser_get_url":
            return _Result("<<<PAGE_CONTENT>>>\nhttps://example.test/apply\n<<<END_PAGE_CONTENT>>>")
        return _Result(f"ok {name}", error=name in self.errors)


class TestNormalizeToolArgs(unittest.TestCase):
    """DeepSeek sometimes emits booleans/numbers as strings (verified against
    a paired replay, 10-07-2026: interactive="True" 26 times in a 10-session
    sample). _normalize_tool_args coerces per each tool's own declared
    schema type instead of guessing generically."""

    def test_boolean_strings_are_coerced_per_schema(self):
        args = transport._normalize_tool_args(
            "browser_snapshot", {"interactive": "True", "compact": "false"})
        self.assertEqual(args, {"interactive": True, "compact": False})

    def test_numeric_strings_are_coerced_to_declared_type(self):
        args = transport._normalize_tool_args(
            "browser_snapshot", {"depth": "3"})
        self.assertEqual(args, {"depth": 3})
        self.assertIsInstance(args["depth"], int)

    def test_already_correct_types_pass_through_unchanged(self):
        args = transport._normalize_tool_args(
            "browser_snapshot", {"interactive": True, "depth": 2})
        self.assertEqual(args, {"interactive": True, "depth": 2})

    def test_unrecognized_string_value_is_left_alone(self):
        # A field whose schema type isn't boolean/integer/number (e.g. the
        # ref/selector string target) must never be touched.
        args = transport._normalize_tool_args(
            "browser_click", {"target": "@e12", "new_tab": "true"})
        self.assertEqual(args, {"target": "@e12", "new_tab": True})

    def test_unmapped_tool_name_is_a_no_op(self):
        args = {"text": "True"}
        self.assertEqual(
            transport._normalize_tool_args("dom_scan", args), args)


class TestAgentBrowserTransport(unittest.IsolatedAsyncioTestCase):
    def test_mcp_is_pinned_to_policy_and_existing_cdp(self):
        params = transport._mcp_params(
            "http://127.0.0.1:9222", "agent_browser", namespace="test-run")
        self.assertEqual(params.command, "agent-browser")
        self.assertEqual(params.env["AGENT_BROWSER_CDP"], "9222")
        self.assertNotIn("http://127.0.0.1:9222", params.args)
        self.assertEqual(params.env["AGENT_BROWSER_NAMESPACE"], "test-run")
        self.assertEqual(params.env["AGENT_BROWSER_CONTENT_BOUNDARIES"], "true")
        self.assertEqual(
            params.env["AGENT_BROWSER_ACTION_POLICY"],
            str(transport.AGENT_BROWSER_ACTION_POLICY),
        )
        self.assertEqual(params.args, ["mcp", "--tools", "all"])
        policy = json.loads(transport.AGENT_BROWSER_ACTION_POLICY.read_text())
        self.assertEqual(policy["default"], "deny")
        self.assertIn("getbytext", policy["allow"])
        self.assertIn("auth_login", policy["allow"])
        self.assertNotIn("eval", policy["allow"])
        self.assertNotIn("network", policy["allow"])
        self.assertNotIn("state", policy["allow"])

    def test_cdp_normalization_preserves_remote_websockets(self):
        self.assertEqual(transport._agent_browser_cdp("http://localhost:9333"), "9333")
        self.assertEqual(
            transport._agent_browser_cdp("wss://browser.example/cdp?token=x"),
            "wss://browser.example/cdp?token=x")

    def test_playwright_rollback_is_version_pinned(self):
        params = transport._mcp_params("http://127.0.0.1:9222", "playwright")
        self.assertEqual(params.command, "npx")
        self.assertIn("@playwright/mcp@0.0.78", params.args)

    def test_model_surface_is_curated_and_normalized(self):
        discovered = [SimpleNamespace(name=name) for name in transport._AGENT_REMOTE_REQUIRED]
        discovered += [SimpleNamespace(name="agent_browser_eval")]
        tools = transport._to_openai_tools(discovered, "agent_browser")
        names = {tool["function"]["name"] for tool in tools}
        self.assertIn("browser_snapshot", names)
        self.assertIn("browser_snapshot_diff", names)
        self.assertIn("browser_find", names)
        self.assertIn("browser_file_upload", names)
        self.assertNotIn("agent_browser_eval", names)
        self.assertNotIn("agent_browser_close", names)

    def test_missing_required_remote_tool_fails_fast(self):
        with self.assertRaisesRegex(RuntimeError, "missing required tools"):
            transport._to_openai_tools([], "agent_browser")

    def test_playwright_surface_keeps_schema_and_filters_raw_javascript(self):
        tools = [
            SimpleNamespace(name="browser_click", description="click", inputSchema={"type": "object"}),
            SimpleNamespace(name="browser_evaluate", description="eval", inputSchema={}),
        ]
        exposed = transport._to_openai_tools(tools, "playwright")
        self.assertEqual([t["function"]["name"] for t in exposed], ["browser_click"])

    async def test_snapshot_defaults_are_compact_interactive_and_include_urls(self):
        session = _Session()
        text, ok = await transport._call_browser_tool(
            session, "agent_browser", "browser_snapshot", {})
        self.assertTrue(ok)
        self.assertIn("agent_browser_snapshot", text)
        self.assertEqual(session.calls, [("agent_browser_snapshot", {
            "interactive": True, "compact": True, "includeUrls": True,
        })])
        session.calls.clear()
        await transport._call_browser_tool(
            session, "agent_browser", "browser_snapshot",
            {"depth": 4, "selector": "main", "interactive": False,
             "compact": False, "include_urls": False})
        self.assertEqual(session.calls[0][1], {
            "interactive": False, "compact": False, "includeUrls": False,
            "depth": 4, "selector": "main",
        })

    async def test_fill_form_batches_without_another_model_turn(self):
        session = _Session()
        text, ok = await transport._call_browser_tool(
            session, "agent_browser", "browser_fill_form", {"fields": [
                {"name": "Email", "target": "@e1", "type": "textbox", "value": "a@example.test"},
                {"name": "Consent", "target": "@e2", "type": "checkbox", "value": True},
            ]})
        self.assertTrue(ok)
        self.assertIn("Email", text)
        self.assertEqual(session.calls[0], (
            "agent_browser_fill", {"selector": "@e1", "text": "a@example.test"}))
        self.assertEqual(session.calls[1], (
            "agent_browser_check", {"selector": "@e2"}))

    async def test_fill_form_maps_every_field_kind_and_stops_on_error(self):
        session = _Session(errors={"agent_browser_select"})
        text, ok = await transport._call_browser_tool(
            session, "agent_browser", "browser_fill_form", {"fields": [
                {"target": "@radio", "type": "radio", "value": "yes"},
                {"target": "@check", "type": "checkbox", "value": False},
                {"target": "@select", "type": "combobox", "value": ["a", "b"]},
                {"target": "@never", "type": "textbox", "value": "not reached"},
            ]})
        self.assertFalse(ok)
        self.assertIn("@select", text)
        self.assertEqual([name for name, _args in session.calls], [
            "agent_browser_click", "agent_browser_uncheck", "agent_browser_select",
        ])

    async def test_direct_actions_map_to_native_argument_names(self):
        session = _Session()
        cases = [
            ("browser_click", {"target": "@e1", "new_tab": True},
             "agent_browser_click", {"selector": "@e1", "newTab": True}),
            ("browser_type", {"target": "@e2", "text": "Ada"},
             "agent_browser_fill", {"selector": "@e2", "text": "Ada"}),
            ("browser_check", {"target": "@e3"},
             "agent_browser_check", {"selector": "@e3"}),
            ("browser_uncheck", {"target": "@e4"},
             "agent_browser_uncheck", {"selector": "@e4"}),
            ("browser_select_option", {"target": "@e5", "values": ["x"]},
             "agent_browser_select", {"selector": "@e5", "values": ["x"]}),
            ("browser_press_key", {"key": "Enter"},
             "agent_browser_press", {"key": "Enter"}),
            ("browser_read_page", {}, "agent_browser_read", {}),
            ("browser_navigate_back", {}, "agent_browser_back", {}),
            ("browser_navigate_forward", {}, "agent_browser_forward", {}),
            ("browser_reload", {}, "agent_browser_reload", {}),
        ]
        for public, args, remote, remote_args in cases:
            session.calls.clear()
            _text, ok = await transport._call_browser_tool(
                session, "agent_browser", public, args)
            self.assertTrue(ok)
            self.assertEqual(session.calls, [(remote, remote_args)])

    async def test_tabs_map_only_action_relevant_arguments(self):
        session = _Session()
        cases = [
            ({"action": "list", "tab": "ignored"}, "agent_browser_tab_list", {}),
            ({"action": "new", "url": "https://x", "label": "portal", "tab": "ignored"},
             "agent_browser_tab_new", {"url": "https://x", "label": "portal"}),
            ({"action": "switch", "tab": "t2", "url": "ignored"},
             "agent_browser_tab_switch", {"tab": "t2"}),
            ({"action": "close", "tab": "t2"},
             "agent_browser_tab_close", {"tab": "t2"}),
        ]
        for args, remote, remote_args in cases:
            session.calls.clear()
            await transport._call_browser_tool(session, "agent_browser", "browser_tabs", args)
            self.assertEqual(session.calls, [(remote, remote_args)])
        with self.assertRaisesRegex(ValueError, "unsupported tab action"):
            await transport._call_browser_tool(
                session, "agent_browser", "browser_tabs", {"action": "destroy"})

    async def test_wait_variants_map_to_typed_native_tools(self):
        session = _Session()
        cases = [
            ({"text": "Done"}, "agent_browser_wait_for_text", {"text": "Done"}),
            ({"text_gone": "Loading"}, "agent_browser_wait_for_text",
             {"text": "Loading", "extraArgs": ["--state", "hidden"]}),
            ({"selector": "#form"}, "agent_browser_wait_for_selector", {"selector": "#form"}),
            ({"url": "**/done"}, "agent_browser_wait_for_url", {"url": "**/done"}),
            ({"load": "networkidle"}, "agent_browser_wait_for_load", {"state": "networkidle"}),
            ({"time": 99}, "agent_browser_wait_ms", {"ms": 10000}),
        ]
        for args, remote, remote_args in cases:
            session.calls.clear()
            await transport._call_browser_tool(
                session, "agent_browser", "browser_wait_for", args)
            self.assertEqual(session.calls, [(remote, remote_args)])

    async def test_find_defaults_missing_action_to_text(self):
        """Verified against a paired replay (10-07-2026): 5 of 6 browser_find
        calls omitted action entirely and errored. A read-only default lets
        the call succeed instead of costing a wasted turn on the error."""
        session = _Session()
        await transport._call_browser_tool(
            session, "agent_browser", "browser_find",
            {"locator": "text", "value": "Ga verder"})
        self.assertEqual(session.calls, [(
            "agent_browser_find", {"locator": "text", "value": "Ga verder", "action": "text"},
        )])
        session.calls.clear()
        # An explicit action must not be overridden.
        await transport._call_browser_tool(
            session, "agent_browser", "browser_find",
            {"locator": "text", "value": "Ga verder", "action": "click"})
        self.assertEqual(session.calls, [(
            "agent_browser_find", {"locator": "text", "value": "Ga verder", "action": "click"},
        )])

    async def test_scroll_dialog_and_diff_mapping(self):
        session = _Session()
        await transport._call_browser_tool(
            session, "agent_browser", "browser_scroll",
            {"direction": "down", "pixels": 450, "selector": "main"})
        self.assertEqual(session.calls[-1], (
            "agent_browser_scroll", {"direction": "down", "amount": 450, "selector": "main"}))
        await transport._call_browser_tool(
            session, "agent_browser", "browser_snapshot_diff",
            {"compact": False, "depth": 3, "selector": "dialog[open]"})
        self.assertEqual(session.calls[-1], (
            "agent_browser_diff_snapshot",
            {"compact": False, "depth": 3, "selector": "dialog[open]"}))
        for action, remote in (("status", "agent_browser_dialog_status"),
                               ("accept", "agent_browser_dialog_accept"),
                               ("dismiss", "agent_browser_dialog_dismiss")):
            await transport._call_browser_tool(
                session, "agent_browser", "browser_handle_dialog",
                {"action": action, "text": "yes"})
            expected = {"text": "yes"} if action == "accept" else {}
            self.assertEqual(session.calls[-1], (remote, expected))
        with self.assertRaisesRegex(ValueError, "unsupported dialog action"):
            await transport._call_browser_tool(
                session, "agent_browser", "browser_handle_dialog", {"action": "click"})
        with self.assertRaisesRegex(ValueError, "not allowed"):
            await transport._call_browser_tool(
                session, "agent_browser", "agent_browser_eval", {})

    async def test_playwright_calls_stay_compatible_and_restrict_uploads(self):
        session = _Session()
        text, ok = await transport._call_browser_tool(
            session, "playwright", "browser_click", {"target": "e1"})
        self.assertTrue(ok)
        self.assertIn("browser_click", text)
        with tempfile.TemporaryDirectory() as tmp:
            document = Path(tmp) / "id.pdf"
            document.write_bytes(b"x")
            with patch.object(transport, "DOCS_DIR", Path(tmp)):
                await transport._call_browser_tool(
                    session, "playwright", "browser_file_upload", {"paths": [str(document)]})
        self.assertEqual(session.calls[-1][0], "browser_file_upload")

    async def test_upload_is_restricted_to_documents_directory(self):
        session = _Session()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp)
            document = root / "proof.pdf"
            document.write_bytes(b"pdf")
            outside = Path(other) / "not-a-document.txt"
            outside.write_text("secret")
            with patch.object(transport, "DOCS_DIR", root):
                _text, ok = await transport._call_browser_tool(
                    session, "agent_browser", "browser_file_upload",
                    {"target": "@e9", "paths": [str(document)]})
                self.assertTrue(ok)
                with self.assertRaisesRegex(ValueError, "outside DOCS_DIR"):
                    await transport._call_browser_tool(
                        session, "agent_browser", "browser_file_upload",
                        {"target": "@e9", "paths": [str(outside)]})
                missing = root / "missing.pdf"
                with self.assertRaisesRegex(ValueError, "does not exist"):
                    transport._allowed_document_paths([str(missing)])

    async def test_current_url_uses_backend_active_tab_without_tab_parsing(self):
        session = _Session()
        self.assertEqual(
            await transport._current_tab_url(session, "agent_browser"),
            "https://example.test/apply")

    async def test_playwright_current_tab_parser_and_failure_are_preserved(self):
        class Tabs(_Session):
            async def call_tool(self, name, args):
                return _Result("- 2: (current) Portal (https://portal.test/apply)")

        self.assertEqual(
            await transport._current_tab_url(Tabs(), "playwright"),
            "https://portal.test/apply")

        class Broken(_Session):
            async def call_tool(self, name, args):
                raise RuntimeError("gone")

        self.assertIsNone(await transport._current_tab_url(Broken(), "playwright"))

    async def test_secure_login_keeps_secret_inside_mcp_calls(self):
        session = _Session()
        text, ok = await transport._secure_login(
            session, "agent_browser", "rental.test",
            "https://auth.test/login",
            {"username": "me@example.test", "password": "top-secret"},
            {"username_selector": "#email", "password_selector": "", "submit_selector": ""},
        )
        self.assertTrue(ok)
        self.assertNotIn("top-secret", text)
        self.assertEqual(session.calls[0][0], "agent_browser_auth_delete")
        self.assertEqual(session.calls[1][0], "agent_browser_auth_save")
        self.assertEqual(session.calls[1][1]["password"], "top-secret")
        self.assertEqual(session.calls[2][0], "agent_browser_auth_login")

    async def test_secure_login_rejects_other_backends_and_save_failure(self):
        text, ok = await transport._secure_login(
            _Session(), "playwright", "x", "https://x", {}, {})
        self.assertFalse(ok)
        self.assertIn("only available", text)
        session = _Session(errors={"agent_browser_auth_save"})
        text, ok = await transport._secure_login(
            session, "agent_browser", "x", "https://x",
            {"username": "u", "password": "p"}, {})
        self.assertFalse(ok)
        self.assertIn("Could not store", text)

    def test_result_flattening_handles_non_text_and_empty_content(self):
        result = SimpleNamespace(content=[SimpleNamespace(text="one"), {"two": 2}])
        self.assertIn("{'two': 2}", transport._result_text(result))
        self.assertEqual(transport._result_text(SimpleNamespace(content=[])), "(no output)")

    def test_watchdog_helpers_and_logger(self):
        self.assertIsInstance(transport._descendant_pids(os.getpid()), list)
        with patch.object(transport, "_descendant_pids", return_value=[123]), \
                patch.object(Path, "read_bytes", return_value=b"agent-browser mcp"), \
                patch.object(transport.os, "kill") as kill:
            self.assertEqual(transport._kill_wedged_children(os.getpid()), [123])
            kill.assert_called_once_with(123, transport.signal.SIGKILL)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.log"
            logger = transport.Logger(path)
            logger.line("hello")
            logger.close()
            logger.close()
            self.assertIn("hello", path.read_text())


class TestToolPagination(unittest.IsolatedAsyncioTestCase):
    async def test_reads_all_pages(self):
        class Session:
            async def list_tools(self, cursor=None):
                if cursor is None:
                    return SimpleNamespace(tools=["a"], nextCursor="next")
                return SimpleNamespace(tools=["b"], nextCursor=None)

        self.assertEqual(await transport._list_mcp_tools(Session()), ["a", "b"])

    async def test_site_controlled_remote_output_gets_content_boundaries(self):
        text, ok = await transport._remote_call(
            _Session(), "agent_browser_snapshot", {"interactive": False})
        self.assertTrue(ok)
        self.assertTrue(text.startswith("<<<PAGE_CONTENT>>>"))
        self.assertTrue(text.endswith("<<<END_PAGE_CONTENT>>>"))

    async def test_control_output_is_not_mislabeled_as_page_content(self):
        text, ok = await transport._remote_call(
            _Session(), "agent_browser_click", {"selector": "#submit"})
        self.assertTrue(ok)
        self.assertNotIn("PAGE_CONTENT", text)


@unittest.skipUnless(shutil.which("agent-browser"), "agent-browser is not installed")
class TestPinnedMcpContract(unittest.IsolatedAsyncioTestCase):
    async def test_real_server_exposes_every_required_pinned_tool(self):
        params = transport._mcp_params(
            "http://127.0.0.1:1", "agent_browser", namespace="contract-test")
        with tempfile.TemporaryDirectory() as sockets:
            params.env = {**(params.env or {}), "AGENT_BROWSER_SOCKET_DIR": sockets}
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    info = await session.initialize()
                    expected = (
                        Path(__file__).parents[1] / "deploy" / "agent-browser.version"
                    ).read_text().strip()
                    self.assertEqual(info.serverInfo.version, expected)
                    discovered = await transport._list_mcp_tools(session)
                    exposed = transport._to_openai_tools(discovered, "agent_browser")
                    self.assertEqual(len(exposed), len(transport._AGENT_BROWSER_TOOLS))


if __name__ == "__main__":
    unittest.main()
