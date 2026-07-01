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
            with patch.object(dedup, "SEEN_FILE", seen), patch.object(dedup, "PROCESSED_FILE", processed):
                store = dedup.SeenStore()
                url = "https://www.huurwoningen.nl/huren/utrecht/abc/?utm_source=mail"
                self.assertTrue(store.is_new(url))

                processed.write_text(
                    json.dumps({"source_url": "https://www.huurwoningen.nl/huren/utrecht/abc/"}) + "\n",
                    encoding="utf-8",
                )
                self.assertFalse(store.is_new(url))


if __name__ == "__main__":
    unittest.main()
