from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import known_gates
from src.dashboard import cache, costs, si


class TestSiRuns(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.run_log = self.root / "self_improvement.jsonl"
        self.log_dir = self.root / "self_improvement"
        self.log_dir.mkdir()
        self._patches = [
            patch.object(si, "SI_RUN_LOG", self.run_log),
            patch.object(si, "SI_LOG_DIR", self.log_dir),
            patch.object(costs, "SI_LOG_DIR", self.log_dir),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        cache.clear()
        self.tmp.cleanup()

    def test_runs_attach_cost_from_matching_log(self):
        (self.log_dir / "20260708_101500.log").write_text(
            "17:00:00 [self-improvement:diagnosis] done estimated_cost_usd=0.012\n"
            "17:05:00 [self-improvement:patch] done estimated_cost_usd=0.020\n",
            encoding="utf-8")
        self.run_log.write_text(json.dumps({
            "ts": "2026-07-08T10:15:05", "event": "done", "status": "blocked",
            "action": "fixed_deployed", "deployed": True, "root_cause": "bug X",
            "log_path": str(self.log_dir / "20260708_101500.log"),
        }) + "\n", encoding="utf-8")
        runs = si.runs()
        self.assertEqual(len(runs), 1)
        self.assertTrue(runs[0]["deployed"])
        self.assertAlmostEqual(runs[0]["cost_usd"], 0.032)
        self.assertEqual(runs[0]["log_name"], "20260708_101500")

    def test_runs_match_log_by_nearest_ts_without_log_path(self):
        (self.log_dir / "20260708_101500.log").write_text(
            "estimated_cost_usd=0.05\n", encoding="utf-8")
        self.run_log.write_text(json.dumps({
            "ts": "2026-07-08T10:15:03", "event": "error", "status": "error",
            "error": "boom",
        }) + "\n", encoding="utf-8")
        runs = si.runs()
        self.assertEqual(runs[0]["action"], "error")
        self.assertTrue(runs[0]["failed"])
        self.assertAlmostEqual(runs[0]["cost_usd"], 0.05)

    def test_kpis_counts_and_skips(self):
        rows = [
            {"ts": "2026-07-08T10:00:00", "event": "done", "action": "fixed_deployed", "deployed": True},
            {"ts": "2026-07-08T10:01:00", "event": "done", "action": "fix_failed", "deployed": False},
            {"ts": "2026-07-08T10:02:00", "event": "skipped_duplicate_incident"},
        ]
        self.run_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        k = si.kpis(days=3650)
        self.assertEqual(k["runs"], 2)
        self.assertEqual(k["deployed"], 1)
        self.assertEqual(k["skipped_duplicates"], 1)
        self.assertEqual(k["landing_rate"], 50.0)


class TestPendingPatches(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "pending_patches"
        self.dir.mkdir()
        self._p = patch.object(si, "PENDING_PATCH_DIR", self.dir)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def test_lists_patches_with_subject_and_git_am(self):
        (self.dir / "20260708_101500_fix.patch").write_text(
            "From abc Mon Sep 17 00:00:00 2001\n"
            "From: Bot <b@x>\nSubject: [PATCH] fix(lock): break stale lock\n\n"
            "diff --git a/x b/x\n", encoding="utf-8")
        patches = si.pending_patches()
        self.assertEqual(len(patches), 1)
        self.assertEqual(patches[0]["subject"], "fix(lock): break stale lock")
        self.assertIn("git am state/pending_patches/", patches[0]["git_am"])

    def test_patch_content_rejects_traversal(self):
        self.assertIsNone(si.patch_content("../../etc/passwd"))
        self.assertIsNone(si.patch_content("nope.txt"))

    def test_patch_content_redacts(self):
        (self.dir / "20260708_x.patch").write_text(
            "password: hunter2secret\n", encoding="utf-8")
        with patch("src.dashboard.data._secret_values", return_value=("hunter2secret",)):
            out = si.patch_content("20260708_x.patch")
        self.assertIsNotNone(out)
        self.assertNotIn("hunter2secret", out)


class TestRunLogValidation(unittest.TestCase):
    def test_run_log_rejects_non_numeric_names(self):
        self.assertIsNone(si.run_log("../secret"))
        self.assertIsNone(si.run_log("abc"))


class TestRemoveGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "known_gates.json"
        self._p = patch.object(known_gates, "GATES_PATH", self.path)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def test_remove_existing_gate(self):
        known_gates.record_gate(domain="your-house.nl", kind="paid_registration", note="€25")
        self.assertEqual(len(known_gates.load_gates()), 1)
        msg = known_gates.remove_gate("your-house.nl", "paid_registration")
        self.assertIn("removed", msg)
        self.assertEqual(known_gates.load_gates(), [])

    def test_remove_missing_gate_raises(self):
        with self.assertRaises(ValueError):
            known_gates.remove_gate("nope.nl", "eligibility")


class TestGatesView(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "known_gates.json"
        self._p = patch.object(known_gates, "GATES_PATH", self.path)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def test_gates_flags_expired(self):
        from datetime import datetime, timedelta
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        known_gates.record_gate(domain="a.nl", kind="account_cap", note="cap", expires_ts=past)
        known_gates.record_gate(domain="b.nl", kind="eligibility", note="students")
        rows = si.gates()
        by_domain = {g["domain"]: g for g in rows}
        self.assertTrue(by_domain["a.nl"]["expired"])
        self.assertFalse(by_domain["b.nl"]["expired"])


if __name__ == "__main__":
    unittest.main()
