"""Regression tests for the two 'unknown'-outcome bugs in browser_agent._run.

Stdlib-only (unittest + unittest.mock), no live DeepSeek/browser needed.
Run: uv run python -m unittest tests/test_browser_agent_loop.py -v
"""
from __future__ import annotations

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from src import browser_agent


class _FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message, finish_reason):
        self.message = message
        self.finish_reason = finish_reason


class _FakeResponse:
    def __init__(self, content=None, tool_calls=None, finish_reason="stop",
                 completion_tokens=5, reasoning_tokens=0):
        self.choices = [_FakeChoice(_FakeMessage(content, tool_calls), finish_reason)]
        self.usage = SimpleNamespace(
            completion_tokens=completion_tokens,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=reasoning_tokens),
        )


class _FakeCompletions:
    """Replays a canned response sequence; repeats the last one once exhausted."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def create(self, **kwargs):
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]


class _FakeAsyncOpenAI:
    def __init__(self, responses):
        self.chat = SimpleNamespace(completions=_FakeCompletions(responses))


class _FakeFunctionCall:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id_, name, arguments="{}"):
        self.id = id_
        self.function = _FakeFunctionCall(name, arguments)


class _FakeToolResult:
    def __init__(self, text):
        self.content = [SimpleNamespace(text=text)]


class _FakeMcpSession:
    async def initialize(self):
        pass

    async def list_tools(self):
        return SimpleNamespace(tools=[])

    async def call_tool(self, name, args):  # pragma: no cover - not exercised
        raise AssertionError(f"unexpected tool call: {name}({args})")


class _FakeMcpSessionPermissive:
    """Like _FakeMcpSession but succeeds for any tool name, for tests that
    need many turns of real browser_snapshot/browser_click calls."""
    async def initialize(self):
        pass

    async def list_tools(self):
        return SimpleNamespace(tools=[])

    async def call_tool(self, name, args):
        return _FakeToolResult(f"ok: {name} {args}")


class _FakeMcpSessionBigSnapshots(_FakeMcpSessionPermissive):
    """Permissive session whose browser_snapshot results are large enough to
    count as page dumps for the stale-dump pruning logic."""
    async def call_tool(self, name, args):
        if name == "browser_snapshot":
            return _FakeToolResult("- generic [ref=eXX]: snapshot line\n" * 200)
        return await super().call_tool(name, args)


class _FakeAsyncCM:
    def __init__(self, value):
        self._value = value

    async def __aenter__(self):
        return self._value

    async def __aexit__(self, *exc):
        return False


class _CollectingLogger:
    def __init__(self):
        self.lines: list[str] = []

    def line(self, s: str) -> None:
        self.lines.append(s)


def _patch_agent(responses, session=None):
    """Patch the OpenAI + MCP boundary so _run runs against canned responses."""
    fake_client = _FakeAsyncOpenAI(responses)
    fake_session = session if session is not None else _FakeMcpSession()
    return (
        patch.object(browser_agent, "AsyncOpenAI", lambda *a, **kw: fake_client),
        patch.object(browser_agent, "stdio_client", lambda params: _FakeAsyncCM((None, None))),
        patch.object(browser_agent, "ClientSession", lambda read, write: _FakeAsyncCM(fake_session)),
    )


class TestExtractOutcome(unittest.TestCase):
    def test_valid_outcome_line(self):
        self.assertEqual(browser_agent._extract_outcome("done.\nOUTCOME: submitted"), "submitted")

    def test_case_insensitive(self):
        self.assertEqual(browser_agent._extract_outcome("OUTCOME: Blocked"), "blocked")

    def test_missing_line(self):
        self.assertIsNone(browser_agent._extract_outcome("Let me take a snapshot."))

    def test_invalid_outcome_word(self):
        self.assertIsNone(browser_agent._extract_outcome("OUTCOME: bogus"))

    def test_empty_text(self):
        self.assertIsNone(browser_agent._extract_outcome(""))
        self.assertIsNone(browser_agent._extract_outcome(None))

    def test_parse_outcome_rc_fallbacks_unaffected(self):
        self.assertEqual(browser_agent._parse_outcome("", 124), "timeout")
        self.assertEqual(browser_agent._parse_outcome("", 2), "error")
        self.assertEqual(browser_agent._parse_outcome("", 1), "incomplete")
        self.assertEqual(browser_agent._parse_outcome("", 0), "unknown")


class TestShouldNudgeSnapshotOveruse(unittest.TestCase):
    def test_below_turn_floor_never_nudges(self):
        self.assertFalse(browser_agent._should_nudge_snapshot_overuse(20, 9))

    def test_few_snapshots_relative_to_turns_no_nudge(self):
        self.assertFalse(browser_agent._should_nudge_snapshot_overuse(2, 20))

    def test_hof_van_oslo_ratio_triggers_well_before_the_end(self):
        # The real transcript ended at 29 snapshots in 60 turns, but at that
        # steady ~1-in-2 ratio the one-shot nudge should have already fired
        # partway through -- e.g. by turn 20 with 14 snapshots so far.
        self.assertTrue(browser_agent._should_nudge_snapshot_overuse(14, 20))

    def test_threshold_boundary(self):
        self.assertTrue(browser_agent._should_nudge_snapshot_overuse(6, 11))
        self.assertFalse(browser_agent._should_nudge_snapshot_overuse(5, 11))


class TestPruneStalePageDumps(unittest.TestCase):
    """Cumulative input tokens grow quadratically if every ~7k-token page dump
    stays in `messages` forever (measured: 6.12M prompt tokens over 60 turns,
    Hof van Oslo 20260701_144029). All but the newest PRUNE_KEEP_RECENT large
    tool results must be stubbed in place."""

    def _tool(self, id_, content):
        return {"role": "tool", "tool_call_id": id_, "content": content}

    def _dump(self, id_):
        return self._tool(id_, "x" * (browser_agent.PRUNE_MIN_CHARS + 1))

    def test_stubs_all_but_newest_two(self):
        messages = [
            {"role": "user", "content": "prompt"},
            self._dump("a"), self._dump("b"), self._dump("c"), self._dump("d"),
        ]
        pruned = browser_agent._prune_stale_page_dumps(messages)
        self.assertEqual(pruned, 2)
        self.assertEqual(messages[1]["content"], browser_agent.STALE_DUMP_STUB)
        self.assertEqual(messages[2]["content"], browser_agent.STALE_DUMP_STUB)
        # Newest two survive intact, ids untouched everywhere.
        self.assertTrue(messages[3]["content"].startswith("x"))
        self.assertTrue(messages[4]["content"].startswith("x"))
        self.assertEqual([m["tool_call_id"] for m in messages[1:]],
                         ["a", "b", "c", "d"])

    def test_small_results_and_non_tool_messages_untouched(self):
        small = self._tool("s", "clicked ok")
        assistant = {"role": "assistant", "content": "y" * 10000}
        messages = [assistant, small, self._dump("a"), self._dump("b")]
        pruned = browser_agent._prune_stale_page_dumps(messages)
        self.assertEqual(pruned, 0)
        self.assertEqual(small["content"], "clicked ok")
        self.assertEqual(len(assistant["content"]), 10000)

    def test_idempotent_across_turns(self):
        messages = [self._dump("a"), self._dump("b"), self._dump("c")]
        self.assertEqual(browser_agent._prune_stale_page_dumps(messages), 1)
        # Next turn adds one more dump: exactly one more gets stubbed.
        messages.append(self._dump("d"))
        self.assertEqual(browser_agent._prune_stale_page_dumps(messages), 1)
        self.assertEqual(
            [m["content"] == browser_agent.STALE_DUMP_STUB for m in messages],
            [True, True, False, False])


class TestClampToolResult(unittest.TestCase):
    def test_short_result_passes_through_unchanged(self):
        self.assertEqual(browser_agent._clamp_tool_result("ok"), "ok")

    def test_long_result_is_cut_and_marked(self):
        """A silent cut makes the model conclude an element below the cap is
        absent from the page -- the truncation must be visible to it."""
        text = "x" * (browser_agent.TOOL_RESULT_MAX_CHARS + 500)
        clamped = browser_agent._clamp_tool_result(text)
        self.assertTrue(clamped.startswith("x" * browser_agent.TOOL_RESULT_MAX_CHARS))
        self.assertIn("truncated", clamped)
        self.assertLess(len(clamped), len(text))

    def test_exactly_at_cap_not_marked(self):
        text = "x" * browser_agent.TOOL_RESULT_MAX_CHARS
        self.assertEqual(browser_agent._clamp_tool_result(text), text)


class TestRunLoop(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._env_patch = patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key"})
        self._env_patch.start()
        self.addAsyncCleanup(self._env_patch.stop)

    async def test_repeated_empty_stop_returns_incomplete(self):
        """Bug A: repeated finish_reason='stop' with empty content and no
        tool_calls (witte-de-withstraat tail). Must exhaust retry+nudge budgets
        and report (1, incomplete), not fake-succeed with (0, unknown)."""
        responses = [_FakeResponse(content=None, tool_calls=None, finish_reason="stop")]
        patches = _patch_agent(responses)
        log = _CollectingLogger()
        with patches[0], patches[1], patches[2]:
            rc, text = await browser_agent._run(
                prompt="test prompt", model="test-model", max_turns=60,
                cdp_url="http://fake", log=log,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(browser_agent._parse_outcome(text, rc), "incomplete")

    async def test_narration_without_outcome_line_triggers_nudge_then_incomplete(self):
        """Bug B: after an initial empty 'stop' (silently retried), the model
        narrates an intended action ('Let me take a snapshot...') with no
        tool_calls and no OUTCOME line (stienstra tail). Must NOT be accepted
        as a done answer immediately -- both nudges should still be spent --
        and must end as (1, incomplete)."""
        responses = [
            _FakeResponse(content=None, tool_calls=None, finish_reason="stop"),
            _FakeResponse(
                content="Let me take a snapshot to see the page content and check eligibility.",
                tool_calls=None, finish_reason="stop",
            ),
        ]
        patches = _patch_agent(responses)
        log = _CollectingLogger()
        with patches[0], patches[1], patches[2]:
            rc, text = await browser_agent._run(
                prompt="test prompt", model="test-model", max_turns=60,
                cdp_url="http://fake", log=log,
            )
        self.assertEqual(rc, 1)
        self.assertEqual(browser_agent._parse_outcome(text, rc), "incomplete")
        nudge_lines = [l for l in log.lines if "[agent] nudge" in l]
        self.assertEqual(len(nudge_lines), 2, "both nudges should have been spent, not skipped")

    async def test_excessive_resnapshotting_triggers_one_shot_nudge(self):
        """Hof van Oslo via REBO Groep (01-07-2026): ~29 of 60 turns were
        browser_snapshot, each following a *different* click, so the
        exact/short-cycle repeat guard never fires (the repeated element is
        the call TYPE, not its arguments). Alternate snapshot/click(unique
        ref) for 11 turns -- the nudge should fire exactly once, at turn 11
        (turn>=10 and snapshot_calls>=max(6, turn//2) => 6>=6)."""
        responses = []
        for i in range(1, 12):
            if i % 2 == 1:
                responses.append(_FakeResponse(
                    tool_calls=[_FakeToolCall(f"id{i}", "browser_snapshot", "{}")]))
            else:
                responses.append(_FakeResponse(tool_calls=[_FakeToolCall(
                    f"id{i}", "browser_click", json.dumps({"target": f"e{i}"}))]))
        responses.append(_FakeResponse(content="Done.\nOUTCOME: blocked",
                                        tool_calls=None, finish_reason="stop"))
        patches = _patch_agent(responses, session=_FakeMcpSessionPermissive())
        log = _CollectingLogger()
        with patches[0], patches[1], patches[2]:
            rc, text = await browser_agent._run(
                prompt="test prompt", model="test-model", max_turns=60,
                cdp_url="http://fake", log=log,
            )
        self.assertEqual(browser_agent._parse_outcome(text, rc), "blocked")
        nudge_lines = [l for l in log.lines if "snapshot-overuse nudge" in l]
        self.assertEqual(len(nudge_lines), 1,
                          "should fire exactly once, not every subsequent turn")

    async def test_stale_snapshots_pruned_during_run(self):
        """Four big snapshots interleaved with unique clicks (so the
        repeat-action guard stays quiet): from the third snapshot on, each
        new dump should stub exactly one older one (keep-newest-2), so the
        context stops growing with every re-snapshot."""
        responses = []
        for i in range(1, 9):
            if i % 2 == 1:
                responses.append(_FakeResponse(
                    tool_calls=[_FakeToolCall(f"id{i}", "browser_snapshot", "{}")]))
            else:
                responses.append(_FakeResponse(tool_calls=[_FakeToolCall(
                    f"id{i}", "browser_click", json.dumps({"target": f"e{i}"}))]))
        responses.append(_FakeResponse(content="Done.\nOUTCOME: blocked",
                                       tool_calls=None, finish_reason="stop"))
        patches = _patch_agent(responses, session=_FakeMcpSessionBigSnapshots())
        log = _CollectingLogger()
        with patches[0], patches[1], patches[2]:
            rc, text = await browser_agent._run(
                prompt="test prompt", model="test-model", max_turns=60,
                cdp_url="http://fake", log=log,
            )
        self.assertEqual(browser_agent._parse_outcome(text, rc), "blocked")
        prune_lines = [l for l in log.lines if "pruned" in l and "stale page dump" in l]
        self.assertEqual(len(prune_lines), 2)

    async def test_dom_scan_and_click_by_text_handled_locally_not_via_mcp(self):
        """dom_scan/click_by_text are local fallback tools (src/
        browser_dom_tools.py) -- must NOT go through session.call_tool
        (the strict _FakeMcpSession raises on any MCP call), and must be
        dispatched to browser_dom_tools with the run's cdp_url."""
        responses = [
            _FakeResponse(tool_calls=[_FakeToolCall("id1", "dom_scan", "{}")]),
            _FakeResponse(tool_calls=[_FakeToolCall(
                "id2", "click_by_text", json.dumps({"text": "Ja, ik ga akkoord"}))]),
            _FakeResponse(content="Done.\nOUTCOME: submitted",
                          tool_calls=None, finish_reason="stop"),
        ]
        patches = _patch_agent(responses)  # strict session: raises on MCP call_tool
        log = _CollectingLogger()
        with patches[0], patches[1], patches[2], \
             patch.object(browser_agent.browser_dom_tools, "dom_scan",
                          AsyncMock(return_value="dom report")) as mock_scan, \
             patch.object(browser_agent.browser_dom_tools, "click_by_text",
                          AsyncMock(return_value="clicked")) as mock_click:
            rc, text = await browser_agent._run(
                prompt="test prompt", model="test-model", max_turns=60,
                cdp_url="http://fake-cdp", log=log,
            )
        self.assertEqual(browser_agent._parse_outcome(text, rc), "submitted")
        # current_url is None here: the strict session rejects the
        # browser_tabs lookup, and _current_tab_url swallows that to None.
        mock_scan.assert_awaited_once_with("http://fake-cdp", current_url=None)
        mock_click.assert_awaited_once_with(
            "http://fake-cdp", "Ja, ik ga akkoord", current_url=None)


if __name__ == "__main__":
    unittest.main()
