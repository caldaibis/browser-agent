from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dashboard import cache, costs, data


class TestJsonlTail(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, rows, mode="w"):
        with self.path.open(mode, encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")

    def test_missing_file_is_empty(self):
        tail = cache.JsonlTail(self.path)
        self.assertEqual(tail.records(), [])

    def test_incremental_append_only_parses_new_lines(self):
        self._write([{"a": 1}, {"a": 2}])
        tail = cache.JsonlTail(self.path)
        self.assertEqual(len(tail.records()), 2)
        # append; a fresh mtime is required for the tail to notice
        time.sleep(0.01)
        self._write([{"a": 3}], mode="a")
        recs = tail.records()
        self.assertEqual([r["a"] for r in recs], [1, 2, 3])

    def test_truncation_triggers_full_reparse(self):
        self._write([{"a": 1}, {"a": 2}, {"a": 3}])
        tail = cache.JsonlTail(self.path)
        self.assertEqual(len(tail.records()), 3)
        time.sleep(0.01)
        self._write([{"b": 9}])  # shrinks the file
        recs = tail.records()
        self.assertEqual(recs, [{"b": 9}])

    def test_malformed_lines_skipped(self):
        self.path.write_text('{"a": 1}\nnot json\n{"a": 2}\n', encoding="utf-8")
        self.assertEqual(len(cache.JsonlTail(self.path).records()), 2)


class TestMemo(unittest.TestCase):
    def setUp(self):
        cache.clear()

    def test_memo_caches_within_ttl(self):
        calls = []
        fn = lambda: (calls.append(1), len(calls))[1]
        self.assertEqual(cache.memo("k", 100, fn), 1)
        self.assertEqual(cache.memo("k", 100, fn), 1)
        self.assertEqual(len(calls), 1)

    def test_memo_recomputes_after_expiry(self):
        calls = []
        fn = lambda: (calls.append(1), len(calls))[1]
        self.assertEqual(cache.memo("k", -1, fn), 1)
        self.assertEqual(cache.memo("k", -1, fn), 2)


class TestStablePermalinks(unittest.TestCase):
    def test_permalink_is_deterministic_and_survives_reordering(self):
        a = data.Submission(id=0, ts="2026-07-08T10:15:03", status="submitted",
                            source="X", address="Straat 1",
                            source_url="https://ex.test/l/1", stekkies_url="",
                            seconds=1.0, message="")
        # Same record content, different line index -> same permalink.
        b = data.Submission(id=42, ts="2026-07-08T10:15:03", status="submitted",
                            source="X", address="Straat 1",
                            source_url="https://ex.test/l/1", stekkies_url="",
                            seconds=1.0, message="")
        self.assertEqual(a.permalink, b.permalink)
        # Different listing -> different permalink.
        c = data.Submission(id=0, ts="2026-07-08T10:15:03", status="submitted",
                            source="X", address="Straat 2",
                            source_url="https://ex.test/l/2", stekkies_url="",
                            seconds=1.0, message="")
        self.assertNotEqual(a.permalink, c.permalink)

    def test_get_submission_resolves_permalink_and_legacy_int(self):
        cache.clear()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mail_summary.jsonl"
            path.write_text(json.dumps({
                "ts": "2026-07-08T10:15:03", "status": "submitted",
                "source_url": "https://ex.test/l/1", "address": "Straat 1",
            }) + "\n", encoding="utf-8")
            with patch.object(data, "MAIL_SUMMARY", path):
                subs = data.load_submissions()
                self.assertEqual(len(subs), 1)
                by_int = data.get_submission("0")
                by_perma = data.get_submission(subs[0].permalink)
                self.assertIsNotNone(by_int)
                self.assertEqual(by_int, by_perma)
        cache.clear()


class TestTrajectoryFirstCost(unittest.TestCase):
    def test_usage_from_trajectory_sums_turn_usage(self):
        cache.clear()
        with tempfile.TemporaryDirectory() as td:
            traj = Path(td) / "trajectories"
            traj.mkdir()
            stem = "20260708_101503_x-straat-1"
            rows = [
                {"event": "run_start", "payload": {"model": "deepseek-v4-pro"}},
                {"event": "turn_usage", "payload": {
                    "turn": 1, "prompt_tokens": 1000, "completion_tokens": 200,
                    "total_tokens": 1200, "cache_hit_tokens": 400,
                    "cache_miss_tokens": 600}},
                {"event": "turn_usage", "payload": {
                    "turn": 2, "prompt_tokens": 500, "completion_tokens": 100,
                    "total_tokens": 600, "cache_hit_tokens": 0,
                    "cache_miss_tokens": 500}},
            ]
            (traj / f"{stem}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            with patch.object(costs, "TRAJECTORY_DIR", traj):
                u = costs.usage_from_trajectory(stem)
        self.assertIsNotNone(u)
        self.assertEqual(u.input_tokens, 1500)
        self.assertEqual(u.output_tokens, 300)
        self.assertEqual(u.cache_hit_tokens, 400)
        self.assertAlmostEqual(
            u.estimated_cost_usd,
            (400 * 0.003625 + 1100 * 0.435 + 300 * 0.87) / 1_000_000,
        )
        cache.clear()

    def test_redacted_token_counts_fall_back_to_none(self):
        # Older trajectory files had token counts redacted to '***'; the parser
        # must not crash and must report unknown so the transcript path is used.
        cache.clear()
        with tempfile.TemporaryDirectory() as td:
            traj = Path(td) / "trajectories"
            traj.mkdir()
            stem = "20260708_redacted"
            rows = [
                {"event": "turn_usage", "payload": {
                    "turn": 1, "prompt_tokens": "***", "completion_tokens": "***"}},
            ]
            (traj / f"{stem}.jsonl").write_text(
                "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
            with patch.object(costs, "TRAJECTORY_DIR", traj):
                self.assertIsNone(costs.usage_from_trajectory(stem))
        cache.clear()


if __name__ == "__main__":
    unittest.main()
