from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import self_improvement_harness as harness


class TestHarnessRedaction(unittest.TestCase):
    def test_record_trajectory_redacts_secret_fields(self):
        with tempfile.TemporaryDirectory() as td, \
             patch.object(harness, "TRAJECTORY_DIR", Path(td)):
            harness.record_trajectory_event("run/1", "tool_call", {
                "username": "person@example.test",
                "password": "super-secret",
                "nested": {"api_key": "key-123"},
            })
            files = list(Path(td).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            text = files[0].read_text(encoding="utf-8")
            self.assertNotIn("super-secret", text)
            self.assertNotIn("key-123", text)
            self.assertIn('"password": "***"', text)

    def test_redact_value_clamps_long_strings(self):
        value = harness.redact_value({"text": "x" * 5000}, max_string=100)
        self.assertIn("truncated at 100 chars", value["text"])


class TestFailureClassification(unittest.TestCase):
    def test_payment_checkout_maps_to_control_policy(self):
        sig = harness.classify_failure(
            "Reached https://www.mollie.com/checkout and stopped.",
            outcome="blocked",
        )
        self.assertEqual(sig.signature, "payment-checkout-hard-stop")
        self.assertEqual(sig.surface, "control_policy")

    def test_dom_fallback_maps_to_tool_registry(self):
        sig = harness.classify_failure(
            "dom_scan showed a dialog; fill_by_label was required.",
            outcome="incomplete",
            domain="rebogroep.nl",
        )
        self.assertEqual(sig.signature, "refless-dialog-dom-fallback")
        self.assertEqual(sig.surface, "tool_registry")
        self.assertEqual(sig.domain, "rebogroep.nl")

    def test_unclassified_maps_to_observability(self):
        sig = harness.classify_failure("unexpected final state", outcome="unknown")
        self.assertEqual(sig.surface, "observability")


class TestEvidenceAndEval(unittest.TestCase):
    def test_mine_failures_clusters_recent_transcripts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            transcripts = root / "transcripts"
            out = root / "evidence"
            transcripts.mkdir()
            (transcripts / "a.log").write_text(
                "dom_scan showed a hidden dialog\nOUTCOME: blocked",
                encoding="utf-8",
            )
            (transcripts / "b.log").write_text(
                "Reached mollie.com checkout\nOUTCOME: blocked",
                encoding="utf-8",
            )
            (transcripts / "c.log").write_text(
                "Submitted successfully\nOUTCOME: submitted",
                encoding="utf-8",
            )
            bundle = harness.mine_failures(
                transcript_dir=transcripts,
                output_dir=out,
                max_files=10,
            )
            self.assertEqual(bundle["record_count"], 2)
            signatures = {c["signature"] for c in bundle["clusters"]}
            self.assertIn("refless-dialog-dom-fallback", signatures)
            self.assertIn("payment-checkout-hard-stop", signatures)
            self.assertEqual(len(list(out.glob("*.json"))), 1)

    def test_eval_harness_uses_fixture_expectations(self):
        summary = harness.eval_harness()
        self.assertGreaterEqual(summary["passed"], 3)
        self.assertEqual(summary["failed"], 0, summary)


if __name__ == "__main__":
    unittest.main()
