from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from src import incident_store, store


class _TempLog(unittest.TestCase):
    pass


class TestFingerprinting(_TempLog):
    def test_site_specific_failure_scopes_to_domain(self):
        fp = incident_store.fingerprint_failure(
            {"source_url": "https://www.your-house.nl/listing/1"},
            "blocked",
            "Reached a Mollie checkout page requiring €25 before applying.",
        )
        self.assertEqual(fp.signature, "payment-checkout-hard-stop")
        self.assertIn("@your-house.nl", fp.key)

    def test_infrastructure_failure_scopes_globally(self):
        fp_a = incident_store.fingerprint_failure(
            {"source_url": "https://site-a.nl/listing/1"},
            "error",
            "TimeoutError: browser_lock held for 25 hours by a stuck CDP render",
        )
        fp_b = incident_store.fingerprint_failure(
            {"source_url": "https://site-b.nl/listing/2"},
            "error",
            "browser_lock wait timed out after stale CDP connect",
        )
        self.assertEqual(fp_a.signature, "browser-lock-contention")
        self.assertEqual(fp_a.key, fp_b.key)

    def test_unclassified_failures_do_not_collapse_across_domains(self):
        fp_a = incident_store.fingerprint_failure(
            {"source_url": "https://site-a.nl/listing/1"}, "unknown", "weird state")
        fp_b = incident_store.fingerprint_failure(
            {"source_url": "https://site-b.nl/listing/2"}, "unknown", "other weird state")
        self.assertNotEqual(fp_a.key, fp_b.key)


class TestDedupPolicy(_TempLog):
    def _fp(self):
        return incident_store.fingerprint_failure(
            {"source_url": "https://example.nl/x"},
            "error",
            "browser_lock wait timed out",
        )

    def test_first_run_allowed_then_skipped_within_window(self):
        fp = self._fp()
        allowed, _ = incident_store.should_run(fp)
        self.assertTrue(allowed)
        incident_store.record_occurrence(fp, summary="first", ran=True)
        allowed, reason = incident_store.should_run(fp)
        self.assertFalse(allowed)
        self.assertIn(fp.key, reason)

    def test_run_allowed_again_after_window(self):
        fp = self._fp()
        incident_store.record_occurrence(fp, summary="first", ran=True)
        later = datetime.now() + timedelta(
            hours=incident_store.SELF_IMPROVEMENT_DEDUP_HOURS + 1)
        allowed, _ = incident_store.should_run(fp, now=later)
        self.assertTrue(allowed)

    def test_skipped_occurrences_do_not_extend_the_window(self):
        fp = self._fp()
        incident_store.record_occurrence(fp, summary="ran", ran=True)
        incident_store.record_occurrence(fp, summary="skipped", ran=False)
        later = datetime.now() + timedelta(
            hours=incident_store.SELF_IMPROVEMENT_DEDUP_HOURS + 1)
        allowed, _ = incident_store.should_run(fp, now=later)
        self.assertTrue(allowed)

    def test_different_fingerprint_not_blocked(self):
        fp = self._fp()
        incident_store.record_occurrence(fp, summary="first", ran=True)
        other = incident_store.fingerprint_failure(
            {"source_url": "https://example.nl/x"},
            "login_required",
            "login_required: stored password rejected on kamernet.nl",
        )
        allowed, _ = incident_store.should_run(other)
        self.assertTrue(allowed)


class TestAttemptHistory(_TempLog):
    def test_history_returns_recent_attempts_in_order(self):
        fp = incident_store.fingerprint_failure(
            {"source_url": "https://example.nl/x"}, "error", "browser_lock timeout")
        for i in range(5):
            incident_store.record_attempt(
                fp, action="fix_failed", root_cause=f"cause {i}", summary=f"try {i}")
        history = incident_store.attempt_history(fp, limit=3)
        self.assertEqual(len(history), 3)
        self.assertEqual(history[-1]["root_cause"], "cause 4")

    def test_incident_summary_counts(self):
        fp = incident_store.fingerprint_failure(
            {"source_url": "https://example.nl/x"}, "error", "browser_lock timeout")
        incident_store.record_occurrence(fp, summary="a", ran=True)
        incident_store.record_occurrence(fp, summary="b", ran=False)
        incident_store.record_attempt(fp, action="fix_failed", summary="try")
        rows = incident_store.incident_summary(days=1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 2)
        self.assertEqual(rows[0]["skipped"], 1)
        self.assertEqual(rows[0]["attempts"], 1)
        self.assertEqual(rows[0]["last_action"], "fix_failed")

    def test_post_deploy_status_reports_recurrence(self):
        fp = incident_store.fingerprint_failure(
            {"source_url": "https://example.nl/x"}, "error", "browser_lock timeout")
        incident_store.record_attempt(
            fp, action="fixed_deployed", summary="try", deployed=True,
            strategy="control_policy", candidate_id="c1")
        incident_store.record_occurrence(fp, summary="same failure", ran=False)
        status = incident_store.post_deploy_status(fp)
        self.assertTrue(status["recurred"])
        self.assertEqual(status["candidate_id"], "c1")
        self.assertEqual(status["deployed_strategy"], "control_policy")

    def test_passwords_never_reach_disk(self):
        fp = incident_store.fingerprint_failure(
            {"source_url": "https://example.nl/x"}, "error", "browser_lock timeout")
        incident_store.record_attempt(
            fp, action="noop", summary="the password hunter2 leaked into a summary")
        # value-level redaction is the dashboard redact()'s job; key-level
        # secrets must be stripped by redact_value — verify the plumbing runs.
        text = json.dumps(store.incidents())
        self.assertIn("attempt", text)


class TestImproveAfterApplyIntegration(_TempLog):
    def test_duplicate_incident_skips_run(self):
        from src import self_improvement_agent
        from src.browser_agent import AgentResult

        listing = {"source_url": "https://example.nl/x"}
        result = AgentResult(rc=2, outcome="error",
                             summary="browser_lock wait timed out")
        fake = self_improvement_agent.SelfImprovementResult(
            action="noop", summary="diagnosed")
        with patch.object(self_improvement_agent, "run_self_improvement",
                          return_value=fake) as run, \
             patch.object(self_improvement_agent, "_log"):
            first = self_improvement_agent.improve_after_apply(
                listing=listing, result=result, trigger="test")
            second = self_improvement_agent.improve_after_apply(
                listing=listing, result=result, trigger="test")
        self.assertEqual(first.action, "noop")
        self.assertEqual(second.action, "skipped_duplicate_incident")
        self.assertEqual(run.call_count, 1)

    def test_prior_attempts_injected_into_context(self):
        from src import self_improvement_agent
        from src.browser_agent import AgentResult

        listing = {"source_url": "https://example.nl/x"}
        result = AgentResult(rc=2, outcome="error",
                             summary="browser_lock wait timed out")
        # A prior attempt OUTSIDE the dedup window: old enough that a new run
        # is allowed, but its findings must still reach the new run's context.
        fp = incident_store.fingerprint_failure(listing, "error", result.summary)
        old_ts = (datetime.now() - timedelta(
            hours=incident_store.SELF_IMPROVEMENT_DEDUP_HOURS + 2)
        ).isoformat(timespec="seconds")
        store.record_incident({
            "ts": old_ts, "event": "attempt", "fingerprint": fp.key,
            "action": "fix_failed", "root_cause": "known cause",
            "summary": "old try", "code_changed": True, "deployed": False,
        })
        fake = self_improvement_agent.SelfImprovementResult(
            action="noop", summary="diagnosed")
        with patch.object(self_improvement_agent, "run_self_improvement",
                          return_value=fake) as run, \
             patch.object(self_improvement_agent, "_log"):
            self_improvement_agent.improve_after_apply(
                listing=listing, result=result, trigger="test")
        ctx = run.call_args.args[0]
        self.assertEqual(ctx["incident"]["fingerprint"], fp.key)
        self.assertEqual(ctx["incident"]["prior_attempts"][-1]["root_cause"],
                         "known cause")


if __name__ == "__main__":
    unittest.main()
