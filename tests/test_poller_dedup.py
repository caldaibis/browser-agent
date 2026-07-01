from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.poller import dedup


class TestSeenStore(unittest.TestCase):
    def test_is_new_refreshes_processed_file(self):
        with tempfile.TemporaryDirectory() as td:
            seen = Path(td) / "seen.jsonl"
            processed = Path(td) / "processed.jsonl"
            claims = Path(td) / "claims.jsonl"
            lock = Path(td) / "dedup.lock"
            with (
                patch.object(dedup, "SEEN_FILE", seen),
                patch.object(dedup, "PROCESSED_FILE", processed),
                patch.object(dedup, "CLAIMS_FILE", claims),
                patch.object(dedup, "LOCK_FILE", lock),
            ):
                store = dedup.SeenStore()
                url = "https://www.huurwoningen.nl/huren/utrecht/abc/?utm_source=mail"
                self.assertTrue(store.is_new(url))

                processed.write_text(
                    json.dumps({"source_url": "https://www.huurwoningen.nl/huren/utrecht/abc/"}) + "\n",
                    encoding="utf-8",
                )
                self.assertFalse(store.is_new(url))

    def test_reserve_blocks_duplicate_until_release(self):
        with tempfile.TemporaryDirectory() as td:
            seen = Path(td) / "seen.jsonl"
            processed = Path(td) / "processed.jsonl"
            claims = Path(td) / "claims.jsonl"
            lock = Path(td) / "dedup.lock"
            with (
                patch.object(dedup, "SEEN_FILE", seen),
                patch.object(dedup, "PROCESSED_FILE", processed),
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
            processed = Path(td) / "processed.jsonl"
            claims = Path(td) / "claims.jsonl"
            lock = Path(td) / "dedup.lock"
            with (
                patch.object(dedup, "SEEN_FILE", seen),
                patch.object(dedup, "PROCESSED_FILE", processed),
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
