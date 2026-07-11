from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from src.dashboard import cache, data, funnel


def _ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


class TestFunnel(unittest.TestCase):
    def setUp(self):
        cache.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.mail = self.root / "mail_summary.jsonl"
        self._patches = [
            patch.object(data, "MAIL_SUMMARY", self.mail),
            patch.object(data, "TRANSCRIPTS_DIR", self.root / "transcripts"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        cache.clear()
        self.tmp.cleanup()

    def _write(self, path, rows):
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")

    def test_mail_funnel_by_trigger(self):
        ts = _ts()
        self._write(self.mail, [
            {"ts": ts, "trigger": "stekkies_mail", "status": "submitted", "msg_id": "a"},
            {"ts": ts, "trigger": "stekkies_mail", "status": "blocked", "msg_id": "b"},
        ])
        rows = {r["trigger"]: r for r in funnel.mail_funnel(days=7)}
        self.assertEqual(rows["Stekkies mail"]["attempted"], 2)
        self.assertEqual(rows["Stekkies mail"]["submitted"], 1)

    def test_failure_pareto_excludes_submitted(self):
        ts = _ts()
        self._write(self.mail, [
            {"ts": ts, "trigger": "huurwoningen_mail", "status": "submitted", "source_url": "https://x.nl/1"},
            {"ts": ts, "trigger": "huurwoningen_mail", "status": "blocked", "source_url": "https://x.nl/2"},
            {"ts": ts, "trigger": "huurwoningen_mail", "status": "blocked", "source_url": "https://x.nl/3"},
            {"ts": ts, "trigger": "huurwoningen_mail", "status": "incomplete", "source_url": "https://x.nl/4"},
        ])
        pareto = dict(funnel.failure_pareto(days=7))
        self.assertEqual(pareto.get("blocked"), 2)
        self.assertEqual(pareto.get("incomplete"), 1)
        self.assertNotIn("submitted", pareto)


if __name__ == "__main__":
    unittest.main()
