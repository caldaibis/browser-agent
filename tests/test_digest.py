from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src import digest


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.mail = root / "mail_summary.jsonl"
        self.si = root / "self_improvement.jsonl"
        self.traj = root / "trajectories"
        self.patches = root / "pending_patches"
        self.gates = root / "known_gates.json"
        self._patches = [
            patch.object(digest, "MAIL_SUMMARY_LOG", self.mail),
            patch.object(digest, "SELF_IMPROVEMENT_LOG", self.si),
            patch.object(digest, "TRAJECTORY_DIR", self.traj),
            patch.object(digest, "PENDING_PATCH_DIR", self.patches),
            patch.object(digest.known_gates, "GATES_PATH", self.gates),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_digest_aggregates_all_sources(self):
        now = datetime.now().isoformat(timespec="seconds")
        _write_jsonl(self.mail, [
            {"ts": now, "trigger": "poller", "status": "submitted"},
            {"ts": now, "trigger": "poller", "status": "incomplete"},
            {"ts": now, "trigger": "stekkies_mail", "status": "submitted"},
            {"ts": "2020-01-01T00:00:00", "trigger": "poller", "status": "old"},
        ])
        _write_jsonl(self.si, [
            {"ts": now, "event": "done", "action": "fix_failed", "deployed": False},
            {"ts": now, "event": "done", "action": "fixed_deployed", "deployed": True},
            {"ts": now, "event": "skipped_duplicate_incident"},
            {"ts": now, "event": "error"},
        ])
        _write_jsonl(self.traj / "run1.jsonl", [
            {"ts": now, "event": "guard", "payload": {"name": "snapshot_overuse_nudge"}},
            {"ts": now, "event": "guard", "payload": {"name": "grace_turns"}},
            {"ts": now, "event": "tool_call", "payload": {"tool": "browser_snapshot"}},
        ])
        self.patches.mkdir(parents=True)
        (self.patches / "20260707_fix.patch").write_text("patch", encoding="utf-8")

        stats = digest.digest_stats(days=7)
        self.assertEqual(stats["outcomes"]["poller"]["submitted"], 1)
        self.assertNotIn("old", stats["outcomes"].get("poller", {}))
        self.assertEqual(stats["guards"]["snapshot_overuse_nudge"], 1)
        self.assertEqual(stats["self_improvement"]["runs"], 3)
        self.assertEqual(stats["self_improvement"]["deployed"], 1)
        self.assertEqual(stats["self_improvement"]["skipped_duplicates"], 1)
        self.assertEqual(len(stats["pending_patches"]), 1)

        text = digest.build_digest(days=7)
        self.assertIn("UNLANDED VERIFIED FIXES", text)
        self.assertIn("snapshot_overuse_nudge", text)
        self.assertIn("stekkies_mail", text)

    def test_digest_handles_missing_logs(self):
        text = digest.build_digest(days=7)
        self.assertIn("LISTING OUTCOMES", text)
        self.assertIn("(none)", text)


class TestHealthcheckProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _write(self, actions: list[str]) -> None:
        rows = []
        for a in actions:
            if a == "crash":
                rows.append({"ts": "2026-07-07T12:00:00", "event": "error"})
            else:
                rows.append({"ts": "2026-07-07T12:00:00", "event": "done", "action": a})
        _write_jsonl(self.log_dir / "self_improvement.jsonl", rows)

    def test_all_recent_failures_alert_once(self):
        from src import healthcheck

        self._write(["crash", "fix_failed", "timeout", "incomplete", "crash"])
        state: dict = {}
        with patch.object(healthcheck, "LOG_DIR", self.log_dir), \
             patch.object(healthcheck, "send_alert") as alert:
            healthcheck.check_self_improvement(state)
            healthcheck.check_self_improvement(state)
        alert.assert_called_once()
        self.assertTrue(state["si_failing_sent"])

    def test_recent_success_disarms(self):
        from src import healthcheck

        self._write(["crash", "crash", "crash", "crash", "noop"])
        state = {"si_failing_sent": True}
        with patch.object(healthcheck, "LOG_DIR", self.log_dir), \
             patch.object(healthcheck, "send_alert") as alert:
            healthcheck.check_self_improvement(state)
        alert.assert_not_called()
        self.assertFalse(state["si_failing_sent"])

    def test_too_few_runs_do_not_alert(self):
        from src import healthcheck

        self._write(["crash", "crash"])
        state: dict = {}
        with patch.object(healthcheck, "LOG_DIR", self.log_dir), \
             patch.object(healthcheck, "send_alert") as alert:
            healthcheck.check_self_improvement(state)
        alert.assert_not_called()


if __name__ == "__main__":
    unittest.main()
