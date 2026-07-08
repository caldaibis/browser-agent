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
        self.poll = self.root / "poller.jsonl"
        self.mail = self.root / "mail_summary.jsonl"
        self._patches = [
            patch.object(data, "POLL_LOG", self.poll),
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

    def test_funnel_stages_and_leak_flag(self):
        ts = _ts()
        self._write(self.poll, [
            {"ts": ts, "event": "polled", "site": "kamernet.nl", "total": 10, "new": 4},
            {"ts": ts, "event": "filtered_out", "url": "https://kamernet.nl/a", "reason": "price 2000 > 1750"},
            {"ts": ts, "event": "judged_out", "url": "https://kamernet.nl/b", "reason": "too far"},
            {"ts": ts, "event": "qualified", "url": "https://kamernet.nl/c"},
            {"ts": ts, "event": "qualified", "url": "https://leak.nl/x"},
        ])
        # kamernet qualified 1 & submitted 1 (no leak); leak.nl qualified 1, no submit.
        self._write(self.mail, [
            {"ts": ts, "trigger": "poller", "status": "submitted",
             "source_url": "https://www.kamernet.nl/c", "detected_by": "kamernet.nl"},
        ])
        rows = {r["domain"]: r for r in funnel.funnel_by_domain(days=7)}
        self.assertEqual(rows["kamernet.nl"]["seen"], 4)
        self.assertEqual(rows["kamernet.nl"]["filtered"], 1)
        self.assertEqual(rows["kamernet.nl"]["judged"], 1)
        self.assertEqual(rows["kamernet.nl"]["qualified"], 1)
        self.assertEqual(rows["kamernet.nl"]["submitted"], 1)
        self.assertFalse(rows["kamernet.nl"]["leak"])
        self.assertTrue(rows["leak.nl"]["leak"])

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
            {"ts": ts, "trigger": "poller", "status": "submitted", "source_url": "https://x.nl/1"},
            {"ts": ts, "trigger": "poller", "status": "blocked", "source_url": "https://x.nl/2"},
            {"ts": ts, "trigger": "poller", "status": "blocked", "source_url": "https://x.nl/3"},
            {"ts": ts, "trigger": "poller", "status": "incomplete", "source_url": "https://x.nl/4"},
        ])
        pareto = dict(funnel.failure_pareto(days=7))
        self.assertEqual(pareto.get("blocked"), 2)
        self.assertEqual(pareto.get("incomplete"), 1)
        self.assertNotIn("submitted", pareto)

    def test_reason_breakdown_groups_numbers(self):
        ts = _ts()
        self._write(self.poll, [
            {"ts": ts, "event": "filtered_out", "url": "https://x.nl/1", "reason": "price 2000 > 1750"},
            {"ts": ts, "event": "filtered_out", "url": "https://x.nl/2", "reason": "price 1900 > 1750"},
        ])
        rb = funnel.reason_breakdown(days=7)
        # both collapse to the same digit-masked reason
        self.assertEqual(len(rb["filtered"]), 1)
        self.assertEqual(rb["filtered"][0][1], 2)


if __name__ == "__main__":
    unittest.main()
