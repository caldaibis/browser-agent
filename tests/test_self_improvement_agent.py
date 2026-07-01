from __future__ import annotations

import unittest
from unittest.mock import patch

from src import self_improvement_agent


class TestSelfImprovementAgent(unittest.TestCase):
    def test_should_recover_default_failure_outcomes(self):
        for status in ("blocked", "error", "incomplete", "timeout", "not_available"):
            self.assertTrue(self_improvement_agent.should_recover(status))

    def test_should_not_recover_success_or_bookkeeping(self):
        for status in ("submitted", "already_applied", "skipped_duplicate", "no_listing_link"):
            self.assertFalse(self_improvement_agent.should_recover(status))

    def test_parse_final_self_improvement_json(self):
        result = self_improvement_agent._parse_final(
            'SELF_IMPROVEMENT_JSON: {"action":"noop","root_cause":"site unavailable",'
            '"summary":"external state","email_sent":false,'
            '"code_changed":false,"deployed":false}'
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.action, "noop")
        self.assertEqual(result.root_cause, "site unavailable")

    def test_safe_path_refuses_sensitive_documents(self):
        with self.assertRaises(ValueError):
            self_improvement_agent._safe_repo_path("documents/id.pdf")

    def test_diff_paths_restricted_to_repo_code(self):
        self_improvement_agent._validate_diff_paths(
            "--- a/src/apply.py\n"
            "+++ b/src/apply.py\n"
            "@@\n"
            "-old\n"
            "+new\n"
        )
        with self.assertRaises(ValueError):
            self_improvement_agent._validate_diff_paths(
                "--- a/state/agent.env\n"
                "+++ b/state/agent.env\n"
                "@@\n"
                "-old\n"
                "+new\n"
            )

    def test_browser_url_validation(self):
        self.assertTrue(self_improvement_agent._safe_browser_url("https://example.test/listing"))
        self.assertTrue(self_improvement_agent._safe_browser_url("http://example.test/listing"))
        self.assertFalse(self_improvement_agent._safe_browser_url("file:///etc/passwd"))
        self.assertFalse(self_improvement_agent._safe_browser_url("javascript:alert(1)"))

    def test_browser_click_guard_blocks_submit_like_labels(self):
        for label in (
            "Reageer op deze woning",
            "Bezichtiging aanvragen",
            "Submit application",
            "Reactie intrekken",
            "Wachtwoord vergeten",
        ):
            self.assertTrue(self_improvement_agent._blocked_click_label(label))

    def test_browser_click_guard_allows_benign_labels(self):
        for label in ("Alles accepteren", "Meer informatie", "Details bekijken"):
            self.assertFalse(self_improvement_agent._blocked_click_label(label))

    def test_self_improvement_tools_include_browser_diagnostics(self):
        names = {tool["function"]["name"] for tool in self_improvement_agent._tools()}
        self.assertIn("browser_open", names)
        self.assertIn("browser_diagnostics", names)
        self.assertIn("browser_safe_click", names)
        self.assertIn("browser_screenshot", names)

    def test_send_fix_email_includes_listing_context(self):
        context = {
            "listing": {
                "source_url": "https://example.test/listing",
                "address": "Teststraat 1",
                "source_name": "Example",
            },
            "result": {"outcome": "blocked"},
        }
        with patch.object(self_improvement_agent, "send_alert") as send_alert:
            self_improvement_agent._send_fix_email(context, "Changed code.", "commit ok")

        send_alert.assert_called_once()
        subject, body = send_alert.call_args.args
        self.assertIn("self-improvement", subject)
        self.assertIn("Changed code.", body)
        self.assertIn("https://example.test/listing", body)
        self.assertIn("blocked", body)

    def test_legacy_recovery_env_alias(self):
        with patch.dict("os.environ", {
            "RECOVERY_DEPLOY_CMD": "echo legacy",
        }, clear=False):
            self.assertEqual(
                self_improvement_agent._env("SELF_IMPROVEMENT_DEPLOY_CMD", "default"),
                "echo legacy",
            )


if __name__ == "__main__":
    unittest.main()
