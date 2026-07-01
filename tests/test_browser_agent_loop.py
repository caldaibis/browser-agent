"""Regression tests for the two 'unknown'-outcome bugs in browser_agent._run.

Stdlib-only (unittest + unittest.mock), no live DeepSeek/browser needed.
Run: uv run python -m unittest tests/test_browser_agent_loop.py -v
"""
from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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


class _FakeMcpSession:
    async def initialize(self):
        pass

    async def list_tools(self):
        return SimpleNamespace(tools=[])

    async def call_tool(self, name, args):  # pragma: no cover - not exercised
        raise AssertionError(f"unexpected tool call: {name}({args})")


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


def _patch_agent(responses):
    """Patch the OpenAI + MCP boundary so _run runs against canned responses."""
    fake_client = _FakeAsyncOpenAI(responses)
    fake_session = _FakeMcpSession()
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


if __name__ == "__main__":
    unittest.main()
