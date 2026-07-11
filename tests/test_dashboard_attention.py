from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from src.dashboard import cache, healthinfo


_HEALTHY = {
    "services": {"orchestrator": "active", "dashboard": "active"},
    "credit": 10.0, "credit_currency": "USD", "credit_low": False,
    "credit_threshold": 2.0, "stekkies_logged_in": True,
    "low_credit": False, "last_health_check": None,
}


class TestAttentionItems(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.patch_dir = self.root / "pending_patches"
        self.lock = self.root / "browser.lock"
        self.si_log = self.root / "self_improvement.jsonl"
        self._patches = [
            patch.object(healthinfo, "PENDING_PATCH_DIR", self.patch_dir),
            patch.object(healthinfo, "BROWSER_LOCK", self.lock),
            patch.object(healthinfo, "SI_RUN_LOG", self.si_log),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        cache.clear()
        self.tmp.cleanup()

    def _items(self, health=None, gates=None):
        cache.clear()
        health = health or dict(_HEALTHY)
        with patch.object(healthinfo, "health", return_value=health), \
             patch("src.known_gates.load_gates", return_value=gates or []):
            return healthinfo.attention_items()

    def test_all_clear(self):
        self.assertEqual(self._items(), [])

    def test_service_down(self):
        h = dict(_HEALTHY, services={"orchestrator": "failed", "dashboard": "active"})
        items = self._items(health=h)
        self.assertTrue(any("orchestrator" in it["title"] for it in items))
        self.assertEqual(items[0]["severity"], "bad")

    def test_low_credit(self):
        h = dict(_HEALTHY, credit=0.5, credit_low=True)
        self.assertTrue(any("credit" in it["title"].lower() for it in self._items(health=h)))

    def test_logged_out(self):
        h = dict(_HEALTHY, stekkies_logged_in=False)
        self.assertTrue(any("logged out" in it["title"].lower() for it in self._items(health=h)))

    def test_pending_patch(self):
        self.patch_dir.mkdir()
        (self.patch_dir / "20260708_fix.patch").write_text("x", encoding="utf-8")
        items = self._items()
        hit = [it for it in items if "not deployed" in it["title"]]
        self.assertTrue(hit)
        self.assertIn("git am", hit[0]["action"])

    def test_paid_gate(self):
        gates = [{"domain": "your-house.nl", "kind": "paid_registration", "note": "€25"}]
        items = self._items(gates=gates)
        self.assertTrue(any("auto-skipped" in it["title"] for it in items))

    def test_si_failing_streak(self):
        rows = [{"event": "done", "action": "error"}] * 5
        self.si_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        self.assertTrue(any("keeps failing" in it["title"] for it in self._items()))

    def test_si_not_failing_when_recent_success(self):
        rows = [{"event": "done", "action": "error"}] * 4 + [{"event": "done", "action": "noop"}]
        self.si_log.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        self.assertFalse(any("keeps failing" in it["title"] for it in self._items()))

    def test_stuck_browser_lock(self):
        self.lock.write_text("pid=1 holder=tier3:foo t=1", encoding="utf-8")
        old = time.time() - (healthinfo.BROWSER_LOCK_STUCK_SECONDS + 120)
        import os
        os.utime(self.lock, (old, old))
        items = self._items()
        self.assertTrue(any("lock" in it["title"].lower() for it in items))

    def test_fresh_browser_lock_not_flagged(self):
        self.lock.write_text("pid=1 holder=apply:x t=1", encoding="utf-8")
        self.assertFalse(any("lock" in it["title"].lower() for it in self._items()))

class TestHealthCreditCache(unittest.TestCase):
    def setUp(self):
        cache.clear()
        healthinfo._credit_value = None
        healthinfo._credit_checked_monotonic = 0.0

    def tearDown(self):
        cache.clear()
        healthinfo._credit_value = None
        healthinfo._credit_checked_monotonic = 0.0

    def test_non_blocking_health_does_not_refresh_credit(self):
        with patch.object(healthinfo, "service_status", return_value={"dashboard": "active"}), \
             patch.object(healthinfo, "remaining_credit", return_value=(9.0, "USD")) as remaining, \
             patch.object(healthinfo, "login_health", return_value={
                 "stekkies_logged_in": True,
                 "low_credit": False,
                 "last_health_check": None,
             }):
            h = healthinfo.health(refresh_credit_if_stale=False)
        remaining.assert_not_called()
        self.assertIsNone(h["credit"])

    def test_warmed_credit_is_used_without_refreshing(self):
        with patch.object(healthinfo, "remaining_credit", return_value=(9.0, "USD")):
            healthinfo.refresh_credit()
        cache.clear()
        with patch.object(healthinfo, "service_status", return_value={"dashboard": "active"}), \
             patch.object(healthinfo, "remaining_credit") as remaining, \
             patch.object(healthinfo, "login_health", return_value={
                 "stekkies_logged_in": True,
                 "low_credit": False,
                 "last_health_check": None,
             }):
            h = healthinfo.health(refresh_credit_if_stale=False)
        remaining.assert_not_called()
        self.assertEqual(h["credit"], 9.0)
        self.assertEqual(h["credit_currency"], "USD")

    def test_non_blocking_health_uses_stale_credit(self):
        healthinfo._credit_value = (7.0, "USD")
        healthinfo._credit_checked_monotonic = (
            time.monotonic() - healthinfo.CREDIT_CACHE_SECONDS - 60
        )
        with patch.object(healthinfo, "service_status", return_value={"dashboard": "active"}), \
             patch.object(healthinfo, "remaining_credit") as remaining, \
             patch.object(healthinfo, "login_health", return_value={
                 "stekkies_logged_in": True,
                 "low_credit": False,
                 "last_health_check": None,
             }):
            h = healthinfo.health(refresh_credit_if_stale=False)
        remaining.assert_not_called()
        self.assertEqual(h["credit"], 7.0)


if __name__ == "__main__":
    unittest.main()
