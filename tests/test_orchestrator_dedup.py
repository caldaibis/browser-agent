"""Pre-flight duplicate guard in the orchestrator: the deterministic,
zero-cost check that must fire BEFORE any browser/LLM spend.

Regression source (Kaatstraat, 02-07-2026): a Huurwoningen mail and a
Stekkies mail delivered the same flat under two different huurwoningen.nl
URL shapes; the pre-flight check matched neither and two full agent runs
($0.07 total) were spent just to hit the mid-run duplicate guard at the
real landlord site (eenhoornmanagement.nl).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import orchestrator, store
from src.models import ProcessedRecord

FRONTEND_URL = ("https://www.huurwoningen.nl/frontend/listing/"
                "8a2c2540-5928-5d33-8e48-f976e65692d0/?alt=x&utm_medium=email")
SITE_PAGE_URL = "https://www.huurwoningen.nl/huren/utrecht/8a2c2540/kaatstraat/"
RESOLVED_URL = ("https://www.eenhoornmanagement.nl/nl/woning/utrecht/"
                "bemuurde-weerd-wz-324/658c1aba08fa71de64c86acd")


class TestPreflightDuplicateGuard(unittest.TestCase):
    def _keys_from(self, record: dict) -> set[str]:
        store.record_processed(ProcessedRecord.from_json(record))
        return orchestrator._processed_keys()

    def test_resolved_url_is_a_preflight_key(self):
        """A mail pointing straight at the real landlord site must be caught
        when an earlier run only recorded that site as resolved_url."""
        keys = self._keys_from({"source_url": FRONTEND_URL,
                                "resolved_url": RESOLVED_URL,
                                "outcome": "already_applied"})
        self.assertTrue(orchestrator._source_duplicate(RESOLVED_URL, keys))

    def test_kaatstraat_regression_both_huurwoningen_shapes_match(self):
        """The exact production miss: mail recorded the /frontend/listing/
        deep-link, Stekkies later delivered the /huren/ site page — the
        listing-id canonicalization must connect them pre-flight."""
        keys = self._keys_from({"source_url": FRONTEND_URL,
                                "outcome": "already_applied"})
        self.assertTrue(orchestrator._source_duplicate(SITE_PAGE_URL, keys))
        self.assertTrue(orchestrator._source_duplicate(FRONTEND_URL, keys))

    def test_different_listing_id_is_not_a_duplicate(self):
        keys = self._keys_from({"source_url": FRONTEND_URL,
                                "outcome": "submitted"})
        other = "https://www.huurwoningen.nl/huren/utrecht/deadbeef/andere-straat/"
        self.assertFalse(orchestrator._source_duplicate(other, keys))

    def test_prevented_message_names_the_guard_and_key(self):
        msg = orchestrator._prevented_message(SITE_PAGE_URL)
        self.assertIn("deterministic duplicate guard", msg)
        self.assertIn("https://huurwoningen.nl/listing/8a2c2540", msg)


class TestPreventedRowsReachTheDashboard(unittest.TestCase):
    def test_skipped_duplicate_summary_is_loaded_as_a_submission_row(self):
        """A deterministically-prevented duplicate must stay visible: the
        dashboard's submissions list is built straight from mail_summary
        records, with no status filter that could hide skipped_duplicate."""
        from src.dashboard import data as dashboard_data
        rec = {"ts": "2026-07-02T08:00:00", "msg_id": "m1",
               "trigger": "stekkies_mail", "source_url": SITE_PAGE_URL,
               "source": "Huurwoningen", "address": "Appartement Kaatstraat",
               "status": "skipped_duplicate",
               "message": orchestrator._prevented_message(SITE_PAGE_URL)}
        with tempfile.TemporaryDirectory() as td:
            summary = Path(td) / "mail_summary.jsonl"
            summary.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            with patch.object(dashboard_data, "MAIL_SUMMARY", summary):
                subs = dashboard_data.load_submissions()
        self.assertEqual(len(subs), 1)
        self.assertEqual(subs[0].status, "skipped_duplicate")
        self.assertIn("deterministic duplicate guard", subs[0].message)


if __name__ == "__main__":
    unittest.main()
