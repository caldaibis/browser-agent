"""SQLite state store + typed records: dedup keys, deletion, incidents."""
from __future__ import annotations

import unittest

from src import store
from src.models import Listing, ProcessedRecord


class TestModels(unittest.TestCase):
    def test_listing_requires_source_url(self):
        with self.assertRaises(ValueError):
            Listing.from_json({"address": "Teststraat 1"})

    def test_listing_roundtrip_ignores_unknown_keys(self):
        listing = Listing.from_json({
            "source_url": "https://example.nl/woning/1",
            "source_name": "example",
            "address": "Teststraat 1",
            "bogus_key": "ignored",
        })
        self.assertEqual(listing.address, "Teststraat 1")
        self.assertNotIn("bogus_key", listing.to_json())

    def test_listing_accepts_legacy_source_alias(self):
        listing = Listing.from_json(
            {"source_url": "https://example.nl/1", "source": "poller-site"})
        self.assertEqual(listing.source_name, "poller-site")

    def test_processed_record_keys_cover_all_url_fields(self):
        rec = ProcessedRecord.from_json({
            "source_url": "https://www.example.nl/woning/1/?utm_source=mail",
            "stekkies_url": "https://stekkies.com/redirect/abc",
            "resolved_url": "https://landlord.nl/woning/1",
        })
        keys = rec.keys()
        # Raw forms present…
        self.assertIn("https://www.example.nl/woning/1/?utm_source=mail", keys)
        self.assertIn("https://landlord.nl/woning/1", keys)
        # …and canonical forms (tracking stripped, www dropped).
        self.assertIn("https://example.nl/woning/1", keys)

    def test_kaatstraat_shapes_share_a_canonical_key(self):
        frontend = ProcessedRecord.from_json({
            "source_url": "https://www.huurwoningen.nl/frontend/listing/"
                          "8a2c2540-5928-5d33-8e48-f976e65692d0/?alt=x"})
        site_page = ProcessedRecord.from_json({
            "source_url": "https://www.huurwoningen.nl/huren/utrecht/8a2c2540/kaatstraat/"})
        self.assertTrue(frontend.keys() & site_page.keys())


class TestStore(unittest.TestCase):
    def test_record_and_query_processed(self):
        rec = ProcessedRecord.from_json({
            "source_url": "https://example.nl/woning/1",
            "resolved_url": "https://landlord.nl/woning/1",
            "outcome": "submitted",
        })
        store.record_processed(rec)
        keys = store.processed_keys()
        self.assertIn("https://example.nl/woning/1", keys)
        self.assertIn("https://landlord.nl/woning/1", keys)
        records = store.processed_records()
        self.assertEqual(records[0].outcome, "submitted")
        self.assertTrue(records[0].ts)

    def test_duplicate_keys_do_not_error(self):
        rec = ProcessedRecord.from_json({"source_url": "https://example.nl/1"})
        store.record_processed(rec)
        store.record_processed(rec)  # INSERT OR IGNORE on listing_keys
        self.assertEqual(len(store.processed_records()), 2)

    def test_delete_processed_removes_record_and_all_keys(self):
        rec = ProcessedRecord.from_json({
            "source_url": "https://example.nl/1",
            "stekkies_url": "https://stekkies.com/redirect/abc",
            "resolved_url": "https://landlord.nl/1",
        })
        store.record_processed(rec)
        self.assertEqual(store.delete_processed(rec.stekkies_url), 1)
        self.assertEqual(store.processed_records(), [])
        self.assertEqual(store.processed_keys(), set())
        self.assertEqual(store.delete_processed(rec.stekkies_url), 0)

    def test_incidents_filter_by_fingerprint(self):
        store.record_incident({"event": "occurrence", "fingerprint": "a"})
        store.record_incident({"event": "attempt", "fingerprint": "b"})
        self.assertEqual(len(store.incidents("a")), 1)
        self.assertEqual(len(store.incidents()), 2)


if __name__ == "__main__":
    unittest.main()
