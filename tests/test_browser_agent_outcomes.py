from __future__ import annotations

import unittest

from src import browser_agent


class TestParseOutcome(unittest.TestCase):
    def test_no_credit_rc(self):
        self.assertEqual(
            browser_agent._parse_outcome("API refused", browser_agent.NO_CREDIT_RC),
            "no_credit")

    def test_declared_outcome_wins_over_rc(self):
        self.assertEqual(
            browser_agent._parse_outcome("done\nOUTCOME: submitted", 0),
            "submitted")

    def test_yielded_and_timeout_unchanged(self):
        self.assertEqual(browser_agent._parse_outcome("", 125), "yielded")
        self.assertEqual(browser_agent._parse_outcome("", 124), "timeout")

    def test_payment_required_is_valid_outcome(self):
        self.assertEqual(
            browser_agent._extract_outcome("Stopped before paying.\nOUTCOME: payment_required"),
            "payment_required")


class TestPaymentUrlGuard(unittest.TestCase):
    def test_known_payment_processors(self):
        self.assertTrue(browser_agent._is_payment_url(
            "https://www.mollie.com/checkout/select-method/abc"))
        self.assertTrue(browser_agent._is_payment_url(
            "https://checkout.stripe.com/c/pay/cs_test"))
        self.assertTrue(browser_agent._is_payment_url(
            "https://checkoutshopper-live.adyen.com/checkoutshopper/"))

    def test_normal_listing_url_not_payment(self):
        self.assertFalse(browser_agent._is_payment_url(
            "https://www.huurwoningen.nl/huren/utrecht/abc/test/"))


class TestRecentFormActivity(unittest.TestCase):
    def test_detects_fill_in_recent_window(self):
        history = [(("browser_snapshot", "{}"),)] * 5 + [
            (("browser_fill_form", '{"fields": []}'),),
            (("browser_click", '{"target": "e1"}'),),
        ]
        self.assertTrue(browser_agent._recent_form_activity(history))

    def test_ignores_old_activity_outside_window(self):
        history = [(("browser_type", "{}"),)] + \
            [(("browser_snapshot", "{}"),)] * 10
        self.assertFalse(browser_agent._recent_form_activity(history, window=8))

    def test_navigation_only_is_not_form_activity(self):
        history = [(("browser_navigate", '{"url": "x"}'),),
                   (("browser_snapshot", "{}"),)]
        self.assertFalse(browser_agent._recent_form_activity(history))

    def test_empty_history(self):
        self.assertFalse(browser_agent._recent_form_activity([]))


if __name__ == "__main__":
    unittest.main()
