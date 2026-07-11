from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src import session_keeper


class TestClassifiers(unittest.TestCase):
    def test_looks_like_captcha(self):
        self.assertTrue(session_keeper._looks_like_captcha(
            "Verifieer dat je een mens bent voordat je verdergaat"))
        self.assertTrue(session_keeper._looks_like_captcha("Please complete the CAPTCHA"))
        self.assertFalse(session_keeper._looks_like_captcha("Welkom terug!"))

    def test_looks_like_2fa(self):
        self.assertTrue(session_keeper._looks_like_2fa("Voer je verificatiecode in"))
        self.assertTrue(session_keeper._looks_like_2fa("2-Step Verification required"))
        self.assertFalse(session_keeper._looks_like_2fa("Wachtwoord onjuist"))

    def test_looks_like_rejected_credentials(self):
        self.assertTrue(session_keeper._looks_like_rejected_credentials(
            "Onjuist wachtwoord, probeer opnieuw"))
        self.assertTrue(session_keeper._looks_like_rejected_credentials(
            "Incorrect password"))
        self.assertFalse(session_keeper._looks_like_rejected_credentials("Welkom terug!"))

    def test_is_google_sso_button(self):
        self.assertTrue(session_keeper._is_google_sso_button("Ga verder met Google"))
        self.assertTrue(session_keeper._is_google_sso_button("Continue with Google"))
        self.assertFalse(session_keeper._is_google_sso_button("Inloggen met e-mail"))

    def test_is_forgot_password(self):
        self.assertTrue(session_keeper._is_forgot_password("Wachtwoord vergeten?"))
        self.assertTrue(session_keeper._is_forgot_password("Forgot password?"))
        self.assertFalse(session_keeper._is_forgot_password("Inloggen"))


class TestHasAdapter(unittest.TestCase):
    def test_bare_domain(self):
        self.assertTrue(session_keeper.has_adapter("huurwoningen.nl"))

    def test_full_url(self):
        self.assertTrue(session_keeper.has_adapter(
            "https://www.huurwoningen.nl/mijn-huurwoningen/"))

    def test_unregistered_domain(self):
        self.assertFalse(session_keeper.has_adapter("kamernet.nl"))
        self.assertFalse(session_keeper.has_adapter("https://example.nl/x"))


class _TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "session_keeper.json"
        self._patch = patch.object(session_keeper, "STATE_PATH", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()


class TestStateAndCooldown(_TempState):
    def test_no_state_means_no_cooldown(self):
        in_cooldown, st = session_keeper._cooldown_active("huurwoningen.nl")
        self.assertFalse(in_cooldown)
        self.assertEqual(st, {})

    def test_record_success_clears_cooldown(self):
        adapter = session_keeper.ADAPTERS["huurwoningen.nl"]
        session_keeper._record_failure(
            "huurwoningen.nl", adapter, "captcha", "CAPTCHA shown")
        in_cooldown, _ = session_keeper._cooldown_active("huurwoningen.nl")
        self.assertTrue(in_cooldown)

        session_keeper._record_success("huurwoningen.nl", repaired=True)
        in_cooldown, st = session_keeper._cooldown_active("huurwoningen.nl")
        self.assertFalse(in_cooldown)
        self.assertEqual(st.get("consecutive_failures"), 0)
        self.assertFalse(st.get("alert_sent"))

    def test_record_failure_backs_off_and_caps_at_4x(self):
        adapter = session_keeper.ADAPTERS["huurwoningen.nl"]
        for _ in range(6):
            session_keeper._record_failure(
                "huurwoningen.nl", adapter, "credentials_rejected", "bad password")
        st = session_keeper._domain_state("huurwoningen.nl")
        self.assertEqual(st["consecutive_failures"], 6)
        next_after = datetime.fromisoformat(st["next_attempt_after_ts"])
        # Backoff is capped at cooldown_seconds * 4, not 6.
        expected_ceiling = datetime.now() + timedelta(seconds=adapter.cooldown_seconds * 4 + 5)
        self.assertLess(next_after, expected_ceiling)

    def test_record_failure_alert_dedup_same_blocker(self):
        adapter = session_keeper.ADAPTERS["huurwoningen.nl"]
        first = session_keeper._record_failure(
            "huurwoningen.nl", adapter, "captcha", "CAPTCHA shown")
        second = session_keeper._record_failure(
            "huurwoningen.nl", adapter, "captcha", "CAPTCHA shown again")
        self.assertTrue(first)
        self.assertFalse(second)

    def test_record_failure_alerts_again_on_new_blocker_kind(self):
        adapter = session_keeper.ADAPTERS["huurwoningen.nl"]
        session_keeper._record_failure(
            "huurwoningen.nl", adapter, "captcha", "CAPTCHA shown")
        changed = session_keeper._record_failure(
            "huurwoningen.nl", adapter, "credentials_rejected", "bad password")
        self.assertTrue(changed)


class _CountLocator:
    def __init__(self, count: int):
        self._count = count

    def count(self) -> int:
        return self._count


class _ScriptedPage:
    """Minimal Page double for `_run_probe_and_repair`'s orchestration logic.

    Scripted with a sequence of "is logged out" booleans, one consumed per
    `goto()` call (the probe, and -- when the login attempt doesn't resolve
    into a human-gate outcome -- the re-probe). The real DOM-walking login
    drivers (`_login_huurwoningen` et al.) are exercised live, not here (see
    the module docstring / AGENTS.md on this repo's existing boundary for
    Playwright-driving code) -- this double only feeds a stub `login`
    callable so `_run_probe_and_repair`'s branching is what's under test.
    """

    def __init__(self, logged_out_sequence: list[bool]):
        self._sequence = list(logged_out_sequence)
        self.url = "https://www.huurwoningen.nl/mijn-huurwoningen/"
        self._current_logged_out = self._sequence[0] if self._sequence else False
        self.closed = False

    def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        self.url = url
        if self._sequence:
            self._current_logged_out = self._sequence.pop(0)

    def wait_for_timeout(self, ms):
        pass

    def locator(self, selector):
        count = 1 if (self._current_logged_out and "password" in selector) else 0
        return _CountLocator(count)

    def close(self):
        self.closed = True


class _FakeCtx:
    def __init__(self, page):
        self._page = page

    def new_page(self):
        return self._page


def _stub_adapter(login_fn):
    return session_keeper.SiteAdapter(
        domain="huurwoningen.nl",
        probe_url="https://www.huurwoningen.nl/mijn-huurwoningen/",
        login_url="https://www.huurwoningen.nl/account/inloggen/",
        cooldown_seconds=100,
        login=login_fn,
    )


class TestRunProbeAndRepair(_TempState):
    def test_already_logged_in_skips_login_entirely(self):
        calls = []
        adapter = _stub_adapter(lambda page, cred: calls.append(1))
        ctx = _FakeCtx(_ScriptedPage([False]))
        outcome = session_keeper._run_probe_and_repair("huurwoningen.nl", adapter, ctx)
        self.assertEqual(outcome.outcome, "ok")
        self.assertEqual(calls, [])

    def test_human_gate_outcome_alerts_and_skips_self_improvement(self):
        adapter = _stub_adapter(
            lambda page, cred: session_keeper.RepairOutcome(
                "huurwoningen.nl", "captcha", "CAPTCHA shown"))
        with patch("src.notify.send_alert") as mock_alert, \
             patch("src.self_improvement_agent.improve_session_keeper_adapter") as mock_si:
            outcome = session_keeper._run_probe_and_repair(
                "huurwoningen.nl", adapter, _FakeCtx(_ScriptedPage([True])))
        self.assertEqual(outcome.outcome, "captcha")
        mock_alert.assert_called_once()
        mock_si.assert_not_called()

    def test_repeated_same_blocker_alerts_only_once(self):
        adapter = _stub_adapter(
            lambda page, cred: session_keeper.RepairOutcome(
                "huurwoningen.nl", "captcha", "CAPTCHA shown"))
        with patch("src.notify.send_alert") as mock_alert:
            session_keeper._run_probe_and_repair(
                "huurwoningen.nl", adapter, _FakeCtx(_ScriptedPage([True])))
            session_keeper._run_probe_and_repair(
                "huurwoningen.nl", adapter, _FakeCtx(_ScriptedPage([True])))
        self.assertEqual(mock_alert.call_count, 1)

    def test_login_ok_and_reprobe_confirms_repaired(self):
        adapter = _stub_adapter(
            lambda page, cred: session_keeper.RepairOutcome(
                "huurwoningen.nl", "ok", "attempted Google SSO"))
        outcome = session_keeper._run_probe_and_repair(
            "huurwoningen.nl", adapter, _FakeCtx(_ScriptedPage([True, False])))
        self.assertEqual(outcome.outcome, "repaired")
        st = session_keeper._domain_state("huurwoningen.nl")
        self.assertEqual(st["status"], "ok")

    def test_login_ok_but_still_logged_out_is_adapter_error_fed_to_si(self):
        adapter = _stub_adapter(
            lambda page, cred: session_keeper.RepairOutcome(
                "huurwoningen.nl", "ok", "attempted Google SSO"))
        with patch("src.notify.send_alert") as mock_alert, \
             patch("src.self_improvement_agent.improve_session_keeper_adapter") as mock_si:
            outcome = session_keeper._run_probe_and_repair(
                "huurwoningen.nl", adapter, _FakeCtx(_ScriptedPage([True, True])))
        self.assertEqual(outcome.outcome, "adapter_error")
        mock_alert.assert_called_once()
        mock_si.assert_called_once()
        self.assertEqual(mock_si.call_args.kwargs["domain"], "huurwoningen.nl")

    def test_ensure_session_uses_supplied_ctx_without_locking(self):
        adapter_login_calls = []
        with patch.dict(session_keeper.ADAPTERS, {
            "huurwoningen.nl": _stub_adapter(
                lambda page, cred: adapter_login_calls.append(1) or
                session_keeper.RepairOutcome("huurwoningen.nl", "ok", "x")),
        }):
            outcome = session_keeper.ensure_session(
                "huurwoningen.nl", ctx=_FakeCtx(_ScriptedPage([False])))
        self.assertEqual(outcome.outcome, "ok")
        self.assertEqual(adapter_login_calls, [])


class TestEnsureSessionEarlyExits(_TempState):
    def test_no_adapter_returns_immediately(self):
        outcome = session_keeper.ensure_session("kamernet.nl")
        self.assertEqual(outcome.outcome, "skipped_no_adapter")

    def test_disabled_returns_immediately(self):
        with patch.object(session_keeper, "SESSION_KEEPER_ENABLED", False):
            outcome = session_keeper.ensure_session("huurwoningen.nl")
        self.assertEqual(outcome.outcome, "skipped_no_adapter")

    def test_cooldown_skips_without_touching_the_browser(self):
        adapter = session_keeper.ADAPTERS["huurwoningen.nl"]
        session_keeper._record_failure(
            "huurwoningen.nl", adapter, "captcha", "CAPTCHA shown")
        outcome = session_keeper.ensure_session("huurwoningen.nl")
        self.assertEqual(outcome.outcome, "skipped_cooldown")
        self.assertEqual(outcome.detail, "CAPTCHA shown")


if __name__ == "__main__":
    unittest.main()
