from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dashboard import cache, trajectories


class TestLoadTimeline(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self._p = patch.object(trajectories, "TRAJECTORY_DIR", self.dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        cache.clear()
        self.tmp.cleanup()

    def _write(self, stem, rows):
        (self.dir / f"{stem}.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_builds_turns_from_events(self):
        self._write("run1", [
            {"event": "run_start", "payload": {"model": "deepseek-v4-pro"}},
            {"event": "tool_call", "payload": {"turn": 1, "tool": "browser_snapshot", "args": "{}"}},
            {"event": "tool_result", "payload": {"turn": 1, "tool": "browser_snapshot", "ok": True, "chars": 5000}},
            {"event": "turn_usage", "payload": {"turn": 1, "finish_reason": "tool_calls",
                                                "prompt_tokens": 1000, "completion_tokens": 200,
                                                "cache_hit_tokens": 400}},
            {"event": "guard", "payload": {"turn": 2, "name": "snapshot_overuse_nudge"}},
            {"event": "final", "payload": {"turn": 3, "outcome": "submitted", "rc": 0}},
        ])
        tl = trajectories.load_timeline("run1")
        self.assertIsNotNone(tl)
        self.assertEqual(tl.source, "trajectory")
        self.assertEqual(tl.model, "deepseek-v4-pro")
        self.assertEqual(tl.turns[0].turn, 1)
        self.assertEqual(tl.turns[0].calls[0].tool, "browser_snapshot")
        self.assertEqual(tl.turns[0].completion_tokens, 200)
        self.assertTrue(tl.has_tokens)
        self.assertIn("snapshot_overuse_nudge", tl.turns[1].guards)
        self.assertEqual(tl.final_outcome, "submitted")

    def test_missing_file_returns_none(self):
        self.assertIsNone(trajectories.load_timeline("nope"))
        self.assertIsNone(trajectories.load_timeline(""))

    def test_secrets_never_appear_in_timeline(self):
        self._write("run2", [
            {"event": "tool_call", "payload": {"turn": 1, "tool": "fill_by_label",
                                               "args": "password=hunter2secret"}},
        ])
        with patch("src.dashboard.data._secret_values", return_value=("hunter2secret",)):
            tl = trajectories.load_timeline("run2")
        blob = json.dumps([c.detail for c in tl.turns[0].calls])
        self.assertNotIn("hunter2secret", blob)
        self.assertIn("***", blob)


class TestTranscriptFallback(unittest.TestCase):
    def test_parses_agent_lines(self):
        text = (
            "12:00:00 [agent] model=deepseek-v4-pro tools=4 cdp=http://x\n"
            "12:00:01 [agent] turn 1 call browser_navigate {'url': 'https://x'}\n"
            "12:00:02 [agent] turn 1 finish=tool_calls prompt_tokens=900 "
            "completion_tokens=120 total_tokens=1020 (cap=8000)\n"
        )
        tl = trajectories.timeline_from_transcript(text)
        self.assertIsNotNone(tl)
        self.assertEqual(tl.source, "transcript")
        self.assertEqual(tl.model, "deepseek-v4-pro")
        self.assertEqual(tl.turns[0].calls[0].tool, "browser_navigate")
        self.assertEqual(tl.turns[0].completion_tokens, 120)
        self.assertEqual(tl.turns[0].prompt_tokens, 900)

    def test_empty_text_returns_none(self):
        self.assertIsNone(trajectories.timeline_from_transcript(""))
        self.assertIsNone(trajectories.timeline_from_transcript("nothing here"))


if __name__ == "__main__":
    unittest.main()
