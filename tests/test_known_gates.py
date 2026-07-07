from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from src import known_gates


class _TempGates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "known_gates.json"
        self._patch = patch.object(known_gates, "GATES_PATH", self.path)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self.tmp.cleanup()


class TestRecordAndLoad(_TempGates):
    def test_record_and_load_round_trip(self):
        msg = known_gates.record_gate(
            domain="https://www.your-house.nl/listing/1",
            kind="paid_registration",
            note="€25 membership via Mollie before applying",
        )
        self.assertIn("your-house.nl", msg)
        gates = known_gates.load_gates()
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["domain"], "your-house.nl")

    def test_same_domain_kind_updates_instead_of_duplicating(self):
        known_gates.record_gate(domain="a.nl", kind="account_cap", note="old")
        known_gates.record_gate(domain="a.nl", kind="account_cap", note="new")
        gates = known_gates.load_gates()
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]["note"], "new")

    def test_expired_gate_ignored(self):
        past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds")
        known_gates.record_gate(domain="a.nl", kind="account_cap",
                                note="cap", expires_ts=past)
        self.assertEqual(known_gates.load_gates(), [])

    def test_future_expiry_still_active(self):
        future = (datetime.now() + timedelta(days=2)).isoformat(timespec="seconds")
        known_gates.record_gate(domain="a.nl", kind="account_cap",
                                note="cap", expires_ts=future)
        self.assertEqual(len(known_gates.load_gates()), 1)

    def test_invalid_kind_rejected(self):
        with self.assertRaises(ValueError):
            known_gates.record_gate(domain="a.nl", kind="bogus", note="x")

    def test_invalid_domain_rejected(self):
        with self.assertRaises(ValueError):
            known_gates.record_gate(domain="not a domain", kind="eligibility", note="x")

    def test_invalid_expiry_rejected(self):
        with self.assertRaises(ValueError):
            known_gates.record_gate(domain="a.nl", kind="account_cap",
                                    note="x", expires_ts="tomorrow")

    def test_missing_file_fails_open(self):
        self.assertEqual(known_gates.load_gates(), [])
        self.assertIsNone(known_gates.paid_registration_reason("https://a.nl/x"))
        self.assertEqual(known_gates.prompt_warnings("https://a.nl/x"), [])


class TestPipelineConsumption(_TempGates):
    def test_paid_gate_short_circuits_apply_preflight(self):
        from src import apply

        known_gates.record_gate(
            domain="paysite.nl", kind="paid_registration",
            note="€19,95 registration before responding")
        result = apply.apply({
            "source_url": "https://www.paysite.nl/woning/1",
            "source_name": "Paysite",
            "address": "Teststraat 1",
            "price": "€ 1.500 per maand",
        })
        self.assertEqual(result.outcome, "payment_required")
        self.assertIn("paid-registration gate", result.summary)

    def test_non_paid_gate_becomes_prompt_warning(self):
        from src import apply

        known_gates.record_gate(
            domain="capped.nl", kind="account_cap",
            note="max 5 concurrent viewing requests reached")
        prompt = apply.build_prompt({
            "source_url": "https://www.capped.nl/woning/1",
            "source_name": "Capped",
            "address": "Teststraat 1",
            "price": "€ 1.500 per maand",
        })
        self.assertIn("KNOWN GATES", prompt)
        self.assertIn("max 5 concurrent viewing requests", prompt)
        self.assertIn("ACCOUNT LIMIT", prompt)

    def test_paid_gate_never_reaches_prompt_warnings(self):
        known_gates.record_gate(
            domain="paysite.nl", kind="paid_registration", note="€25 fee")
        self.assertEqual(known_gates.prompt_warnings("https://paysite.nl/x"), [])


if __name__ == "__main__":
    unittest.main()
