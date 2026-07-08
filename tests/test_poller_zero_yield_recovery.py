from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src import incident_store, self_improvement_agent as sia


class TestPollerFingerprint(unittest.TestCase):
    def test_fingerprint_scoped_to_site(self):
        a = incident_store.fingerprint_poller_zero_yield("mijndak.nl")
        b = incident_store.fingerprint_poller_zero_yield("rebogroep.nl")
        self.assertEqual(a.key, "poller-zero-yield@mijndak.nl")
        self.assertEqual(a.signature, "poller-zero-yield")
        self.assertNotEqual(a.key, b.key)


class TestImprovePollerZeroYield(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "incidents.jsonl"
        self._p = patch.object(incident_store, "INCIDENT_LOG", self.log)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self.tmp.cleanup()

    def test_disabled_returns_none(self):
        with patch.object(sia, "SELF_IMPROVEMENT_ENABLED", False):
            self.assertIsNone(sia.improve_poller_zero_yield(site_name="mijndak.nl"))

    def test_runs_with_poller_context(self):
        captured = {}

        def _fake_run(ctx):
            captured["ctx"] = ctx
            return sia.SelfImprovementResult(action="fixed_deployed", summary="fixed",
                                             deployed=True)

        with patch.object(sia, "SELF_IMPROVEMENT_ENABLED", True), \
             patch.object(sia, "run_self_improvement", side_effect=_fake_run), \
             patch.object(sia, "_log"):
            rr = sia.improve_poller_zero_yield(
                site_name="mijndak.nl", list_url="https://www.mijndak.nl/woningaanbod/",
                tier=2, sample_path="/tmp/sample.html", streak=120)
        self.assertEqual(rr.action, "fixed_deployed")
        ctx = captured["ctx"]
        self.assertEqual(ctx["kind"], "poller_zero_yield")
        self.assertEqual(ctx["poller"]["site_name"], "mijndak.nl")
        self.assertEqual(ctx["poller"]["sample_path"], "/tmp/sample.html")
        self.assertEqual(ctx["result"]["outcome"], "poller_zero_yield")

    def test_dedups_second_run_within_window(self):
        fake = sia.SelfImprovementResult(action="noop", summary="looked, empty")
        with patch.object(sia, "SELF_IMPROVEMENT_ENABLED", True), \
             patch.object(sia, "run_self_improvement", return_value=fake) as run, \
             patch.object(sia, "_log"):
            first = sia.improve_poller_zero_yield(site_name="mijndak.nl", streak=120)
            second = sia.improve_poller_zero_yield(site_name="mijndak.nl", streak=240)
        self.assertEqual(first.action, "noop")
        self.assertEqual(second.action, "skipped_duplicate_incident")
        self.assertEqual(run.call_count, 1)

    def test_prior_attempts_injected(self):
        fp = incident_store.fingerprint_poller_zero_yield("mijndak.nl")
        old = (datetime.now() - timedelta(
            hours=incident_store.SELF_IMPROVEMENT_DEDUP_HOURS + 2)).isoformat(timespec="seconds")
        import json
        with self.log.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": old, "event": "attempt", "fingerprint": fp.key,
                                "action": "fix_failed", "root_cause": "regex still off",
                                "summary": "prev"}) + "\n")
        captured = {}

        def _fake_run(ctx):
            captured["ctx"] = ctx
            return sia.SelfImprovementResult(action="noop", summary="x")

        with patch.object(sia, "SELF_IMPROVEMENT_ENABLED", True), \
             patch.object(sia, "run_self_improvement", side_effect=_fake_run), \
             patch.object(sia, "_log"):
            sia.improve_poller_zero_yield(site_name="mijndak.nl", streak=120)
        self.assertEqual(captured["ctx"]["incident"]["prior_attempts"][-1]["root_cause"],
                         "regex still off")


class TestPollerPrompts(unittest.TestCase):
    def _ctx(self):
        return {
            "kind": "poller_zero_yield",
            "result": {"outcome": "poller_zero_yield", "summary": "x", "transcript_path": ""},
            "poller": {"site_name": "mijndak.nl", "list_url": "https://www.mijndak.nl/woningaanbod/",
                       "tier": 2, "parser_desc": "", "sample_path": "/tmp/s.html", "streak": 120},
            "incident": {},
        }

    def test_diagnosis_prompt_points_at_parser_and_sample(self):
        p = sia._diagnosis_prompt(self._ctx())
        self.assertIn("mijndak.nl", p)
        self.assertIn("src/poller/parsers.py", p)
        self.assertIn("/tmp/s.html", p)
        self.assertIn("DIAGNOSIS_JSON", p)

    def test_patch_prompt_points_at_registry_and_deploy(self):
        p = sia._patch_prompt(self._ctx(), {"verdict": "fix", "fix_plan": "regex"})
        self.assertIn("src/poller/registry.py", p)
        self.assertIn("commit_push_deploy", p)
        self.assertIn("poll-once", p)

    def test_apply_prompt_unaffected(self):
        p = sia._diagnosis_prompt({"kind": "apply",
            "result": {"outcome": "blocked", "summary": "x", "transcript_path": ""},
            "incident": {}})
        self.assertIn("rental-application", p)


if __name__ == "__main__":
    unittest.main()
