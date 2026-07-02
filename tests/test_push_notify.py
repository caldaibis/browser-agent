from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import push_notify


def _sub(endpoint: str) -> dict:
    return {"endpoint": endpoint, "keys": {"p256dh": "pk", "auth": "a"}}


class TestVapidKeys(unittest.TestCase):
    def test_generated_once_and_persisted(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(push_notify, "VAPID_FILE", Path(td) / "vapid.json"):
                first = push_notify.public_key()
                second = push_notify.public_key()
        self.assertEqual(first, second)
        # applicationServerKey must be the 65-byte uncompressed P-256 point.
        raw = base64.urlsafe_b64decode(first + "=" * (-len(first) % 4))
        self.assertEqual(len(raw), 65)
        self.assertEqual(raw[0], 0x04)


class TestSubscriptionStore(unittest.TestCase):
    def test_add_list_remove_roundtrip_with_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            with patch.object(push_notify, "SUBSCRIPTIONS_FILE",
                              Path(td) / "subs.jsonl"):
                self.assertEqual(push_notify.list_subscriptions(), [])
                push_notify.add_subscription(_sub("https://push/e1"), "Chrome")
                push_notify.add_subscription(_sub("https://push/e1"), "Chrome")
                push_notify.add_subscription(_sub("https://push/e2"), "Android")
                self.assertEqual(len(push_notify.list_subscriptions()), 2)
                push_notify.remove_subscription("https://push/e1")
                remaining = push_notify.list_subscriptions()
                self.assertEqual([s["endpoint"] for s in remaining],
                                 ["https://push/e2"])

    def test_subscription_without_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            push_notify.add_subscription({"keys": {}})


class TestSend(unittest.TestCase):
    def _env(self, td: str):
        return (
            patch.object(push_notify, "VAPID_FILE", Path(td) / "vapid.json"),
            patch.object(push_notify, "SUBSCRIPTIONS_FILE", Path(td) / "subs.jsonl"),
        )

    def test_send_push_reaches_every_subscription(self):
        sent = []
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = self._env(td)
            with p1, p2, patch("pywebpush.webpush",
                               side_effect=lambda **kw: sent.append(kw)):
                push_notify.add_subscription(_sub("https://push/e1"))
                push_notify.add_subscription(_sub("https://push/e2"))
                n = push_notify.send_push("✅ Submitted: Teststraat 1", "body")
        self.assertEqual(n, 2)
        self.assertEqual(len(sent), 2)
        payload = json.loads(sent[0]["data"])
        self.assertEqual(payload["title"], "✅ Submitted: Teststraat 1")

    def test_gone_subscription_is_pruned_and_send_never_raises(self):
        from pywebpush import WebPushException

        class _Resp:
            status_code = 410

        with tempfile.TemporaryDirectory() as td:
            p1, p2 = self._env(td)
            with p1, p2, patch("pywebpush.webpush",
                               side_effect=WebPushException("gone", response=_Resp())):
                push_notify.add_subscription(_sub("https://push/dead"))
                push_notify.send_push("t", "b")  # must not raise
                self.assertEqual(push_notify.list_subscriptions(), [])

    def test_push_status_respects_outcome_filter(self):
        calls = []
        with tempfile.TemporaryDirectory() as td:
            p1, p2 = self._env(td)
            with p1, p2, patch.object(push_notify, "send_push",
                                      side_effect=lambda *a, **kw: calls.append(kw) or 1):
                push_notify.push_status({"status": "incomplete", "address": "x"})
                self.assertEqual(calls, [])
                push_notify.push_status({"status": "submitted",
                                         "address": "Teststraat 1",
                                         "source": "Kamernet",
                                         "message": "Done."})
                self.assertEqual(len(calls), 1)
                self.assertIn("Teststraat 1", calls[0]["title"])


if __name__ == "__main__":
    unittest.main()
