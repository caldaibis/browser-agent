from __future__ import annotations

import unittest

from src.dashboard import data
from src.dashboard.data import Submission, race_report


class TestDashboardTokenUsage(unittest.TestCase):
    def test_parse_modern_usage_lines_with_cache_pricing(self):
        usage = data.parse_token_usage(
            "12:00:00 [agent] model=deepseek-v4-pro tools=4 cdp=http://x\n"
            "12:00:01 [agent] turn 1 finish=tool_calls "
            "prompt_tokens=1000 completion_tokens=200 total_tokens=1200 "
            "reasoning_tokens=30 cache_hit_tokens=400 cache_miss_tokens=600 "
            "(cap=8000)\n"
            "12:00:02 [agent] turn 2 finish=stop "
            "prompt_tokens=500 completion_tokens=100 total_tokens=600 "
            "reasoning_tokens=0 cache_hit_tokens=0 cache_miss_tokens=500 "
            "(cap=8000)\n"
        )

        self.assertIsNotNone(usage)
        self.assertEqual(usage.model, "deepseek-v4-pro")
        self.assertEqual(usage.input_tokens, 1500)
        self.assertEqual(usage.output_tokens, 300)
        self.assertEqual(usage.total_tokens, 1800)
        self.assertEqual(usage.reasoning_tokens, 30)
        self.assertEqual(usage.cache_hit_tokens, 400)
        self.assertEqual(usage.cache_miss_tokens, 1100)
        self.assertFalse(usage.cost_is_partial)
        self.assertAlmostEqual(
            usage.estimated_cost_usd,
            (400 * 0.003625 + 1100 * 0.435 + 300 * 0.87) / 1_000_000,
        )

    def test_parse_legacy_completion_only_lines_as_lower_bound(self):
        usage = data.parse_token_usage(
            "12:00:00 [agent] model=deepseek-v4-pro tools=4 cdp=http://x\n"
            "12:00:01 [agent] turn 1 finish=stop "
            "completion_tokens=250 reasoning_tokens=10 (cap=8000)\n"
        )

        self.assertIsNotNone(usage)
        self.assertIsNone(usage.input_tokens)
        self.assertIsNone(usage.total_tokens)
        self.assertEqual(usage.output_tokens, 250)
        self.assertTrue(usage.cost_is_partial)
        self.assertAlmostEqual(usage.estimated_cost_usd, 250 * 0.87 / 1_000_000)


class TestRaceReport(unittest.TestCase):
    def test_matches_huurwoningen_mail_submission_without_mail_event_cache(self):
        # Same listing, seen first by the poller, then by a Huurwoningen mail
        # trigger. Both now carry the same *resolved* source_url (the mail
        # side used to keep an unresolved track.huurwoningen.nl link, which
        # meant this pairing could never match).
        same_url = "https://www.huurwoningen.nl/huren/utrecht/f4d60b2f/tussenbusstraat/"
        poller_sub = Submission(
            id=1, ts="2026-07-01T14:10:00", status="blocked", source="huurwoningen.nl",
            address="Tussenbusstraat", source_url=same_url, stekkies_url="",
            seconds=200.0, message="", trigger="poller", detected_by="huurwoningen.nl",
            detected_ts="2026-07-01T14:06:36",
        )
        mail_sub = Submission(
            id=2, ts="2026-07-01T15:34:44", status="blocked", source="Huurwoningen",
            address="Huis Tussenbusstraat", source_url=same_url, stekkies_url="",
            seconds=158.5, message="", trigger="huurwoningen_mail",
            msg_id="19f1e4bfe4038a5b", msg_received_ts="2026-07-01T15:28:42",
        )

        race = race_report([poller_sub, mail_sub], mail_events=[])

        self.assertEqual(race["huurwoningen"]["matched"], 1)
        self.assertEqual(race["huurwoningen"]["poller_wins"], 1)
        self.assertTrue(race["rows"][0].poller_won_huurwoningen)

    def test_no_match_when_only_a_poller_submission_exists(self):
        poller_sub = Submission(
            id=1, ts="2026-07-01T14:10:00", status="blocked", source="huurwoningen.nl",
            address="Tussenbusstraat", source_url="https://www.huurwoningen.nl/huren/utrecht/abc/",
            stekkies_url="", seconds=200.0, message="", trigger="poller",
            detected_by="huurwoningen.nl", detected_ts="2026-07-01T14:06:36",
        )

        race = race_report([poller_sub], mail_events=[])

        self.assertEqual(race["huurwoningen"]["matched"], 0)
        self.assertEqual(race["huurwoningen"]["no_mail"], 1)


if __name__ == "__main__":
    unittest.main()
