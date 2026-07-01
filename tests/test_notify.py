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


if __name__ == "__main__":
    unittest.main()
