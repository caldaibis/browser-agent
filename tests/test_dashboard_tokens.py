from __future__ import annotations

import unittest

from src.dashboard import data


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


if __name__ == "__main__":
    unittest.main()
