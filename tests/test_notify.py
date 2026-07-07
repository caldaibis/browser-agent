from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from src import notify


class TestNotify(unittest.TestCase):
    def test_status_email_only_sends_submitted(self):
        with patch.object(notify, "get_service") as get_service:
            for status in (
                "already_applied",
                "not_available",
                "not_eligible",
                "login_required",
                "blocked",
                "timeout",
                "incomplete",
                "error",
                "skipped_duplicate",
                "applied",
            ):
                notify.send_status_email({"status": status, "address": "Teststraat 1"})

        get_service.assert_not_called()

    def test_status_email_sends_submitted(self):
        execute = Mock()
        send = Mock(return_value=Mock(execute=execute))
        messages = Mock(return_value=Mock(send=send))
        users = Mock(return_value=Mock(messages=messages))
        service = Mock(users=users)

        with patch.object(notify, "get_service", return_value=service):
            notify.send_status_email({
                "status": "submitted",
                "address": "Teststraat 1",
                "source": "Pararius",
                "source_url": "https://example.test/listing",
            })

        execute.assert_called_once()


class TestSendAlertDedup(unittest.TestCase):
    def test_rate_limits_per_key(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            dedup_file = Path(tmp) / "alert_dedup.json"
            with patch.object(notify, "ALERT_DEDUP_FILE", dedup_file), \
                    patch.object(notify, "send_alert") as send_alert:
                self.assertTrue(notify.send_alert_dedup("k", "s", "b"))
                self.assertFalse(notify.send_alert_dedup("k", "s", "b"))
                # A different key is independent.
                self.assertTrue(notify.send_alert_dedup("k2", "s", "b"))
            self.assertEqual(send_alert.call_count, 2)

    def test_interval_expiry_re_arms(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            dedup_file = Path(tmp) / "alert_dedup.json"
            with patch.object(notify, "ALERT_DEDUP_FILE", dedup_file), \
                    patch.object(notify, "send_alert"):
                self.assertTrue(notify.send_alert_dedup("k", "s", "b",
                                                        min_interval_s=0.0))
                self.assertTrue(notify.send_alert_dedup("k", "s", "b",
                                                        min_interval_s=0.0))

    def test_alert_pushes_before_email(self):
        with patch.object(notify, "get_service") as get_service:
            get_service.side_effect = RuntimeError("gmail token dead")
            with patch("src.push_notify.send_push") as send_push:
                # Must not raise, and the push must go out even though the
                # email path is dead (the 04-07-2026 failure mode).
                notify.send_alert("subject", "body")
            send_push.assert_called_once()


if __name__ == "__main__":
    unittest.main()
