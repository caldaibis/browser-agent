from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src import store as state_store
from src.models import ProcessedRecord
from src.poller import dedup


class TestCanonicalUrlHuurwoningen(unittest.TestCase):
    """The same huurwoningen.nl listing appears under two unrelated URL
    shapes — the alert-mail deep-link and the site page. Verified live
    (Kaatstraat, 02-07-2026): both must collapse to one key, or the
    pre-flight duplicate check misses and a full agent run is burned just to
    hit the mid-run guard."""

    FRONTEND = ("https://www.huurwoningen.nl/frontend/listing/"
                "8a2c2540-5928-5d33-8e48-f976e65692d0/"
                "?alt=c4b51397-db46-43e3-88d9-cff8db5ce064"
                "&utm_campaign=for_rent&utm_medium=email")
    SITE_PAGE = "https://www.huurwoningen.nl/huren/utrecht/8a2c2540/kaatstraat/"

    def test_both_shapes_collapse_to_the_same_listing_key(self):
        key = dedup.canonical_url(self.FRONTEND)
        self.assertEqual(key, "https://huurwoningen.nl/listing/8a2c2540")
        self.assertEqual(dedup.canonical_url(self.SITE_PAGE), key)

    def test_recanonicalizing_an_old_stored_key_is_stable(self):
        """Readers re-canonicalize keys stored before this rule existed; the
        old canonical form must map to the new key too."""
        old_key = "https://huurwoningen.nl/huren/utrecht/8a2c2540/kaatstraat"
        self.assertEqual(dedup.canonical_url(old_key),
                         "https://huurwoningen.nl/listing/8a2c2540")

    def test_non_listing_huurwoningen_pages_unaffected(self):
        # City overview (no listing id) and a non-hex slug keep normal keying.
        self.assertEqual(dedup.canonical_url("https://www.huurwoningen.nl/huren/utrecht/"),
                         "https://huurwoningen.nl/huren/utrecht")
        self.assertEqual(
            dedup.canonical_url("https://www.huurwoningen.nl/huren/utrecht/kaatstraat/x/"),
            "https://huurwoningen.nl/huren/utrecht/kaatstraat/x")

    def test_other_hosts_unaffected(self):
        self.assertEqual(
            dedup.canonical_url("https://www.pararius.nl/huren/utrecht/8a2c2540/x/"),
            "https://pararius.nl/huren/utrecht/8a2c2540/x")


class TestSeenStore(unittest.TestCase):
    def test_is_new_refreshes_processed_store(self):
        with tempfile.TemporaryDirectory() as td:
            seen = Path(td) / "seen.jsonl"
            claims = Path(td) / "claims.jsonl"
            lock = Path(td) / "dedup.lock"
            with (
                patch.object(dedup, "SEEN_FILE", seen),
                patch.object(dedup, "CLAIMS_FILE", claims),
                patch.object(dedup, "LOCK_FILE", lock),
            ):
                store = dedup.SeenStore()
                url = "https://www.huurwoningen.nl/huren/utrecht/abc/?utm_source=mail"
                self.assertTrue(store.is_new(url))

                state_store.record_processed(ProcessedRecord.from_json({
                    "source_url": "https://www.huurwoningen.nl/huren/utrecht/abc/",
                }))
                self.assertFalse(store.is_new(url))

    def test_reserve_blocks_duplicate_until_release(self):
        with tempfile.TemporaryDirectory() as td:
            seen = Path(td) / "seen.jsonl"
            claims = Path(td) / "claims.jsonl"
            lock = Path(td) / "dedup.lock"
            with (
                patch.object(dedup, "SEEN_FILE", seen),
                patch.object(dedup, "CLAIMS_FILE", claims),
                patch.object(dedup, "LOCK_FILE", lock),
            ):
                store = dedup.SeenStore()
                url = "https://www.pararius.nl/appartement-te-huur/utrecht/abc123/"

                self.assertTrue(store.reserve(url))
                self.assertFalse(store.is_new(url))
                self.assertFalse(store.reserve(url))
                self.assertIn(dedup.canonical_url(url), dedup.active_claim_keys())

                store.release(url)
                self.assertTrue(store.is_new(url))

    def test_canonical_url_ignores_www_and_tracking(self):
        a = "https://www.pararius.nl/huis-te-huur/utrecht/abc/?utm_source=x"
        b = "https://pararius.nl/huis-te-huur/utrecht/abc/"
        self.assertEqual(dedup.canonical_url(a), dedup.canonical_url(b))

    def test_release_count_tracks_repeated_non_terminal_failures(self):
        with tempfile.TemporaryDirectory() as td:
            seen = Path(td) / "seen.jsonl"
            claims = Path(td) / "claims.jsonl"
            lock = Path(td) / "dedup.lock"
            with (
                patch.object(dedup, "SEEN_FILE", seen),
                patch.object(dedup, "CLAIMS_FILE", claims),
                patch.object(dedup, "LOCK_FILE", lock),
            ):
                store = dedup.SeenStore()
                url = "https://www.huurwoningen.nl/huren/utrecht/0555f33c/hof-van-oslo/"

                self.assertEqual(dedup.release_count(url), 0)
                store.reserve(url)
                store.release(url)
                self.assertEqual(dedup.release_count(url), 1)
                store.reserve(url)
                store.release(url)
                self.assertEqual(dedup.release_count(url), 2)
                # A different listing's releases don't bleed into this count.
                other = "https://www.huurwoningen.nl/huren/utrecht/abc/other/"
                store.reserve(other)
                store.release(other)
                self.assertEqual(dedup.release_count(url), 2)


if __name__ == "__main__":
    unittest.main()
