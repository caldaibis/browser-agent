"""Settings loader: defaults, parsing, validation errors, legacy aliases."""
from __future__ import annotations

import unittest

from src.settings import Settings, SettingsError, load_settings


class TestLoadSettings(unittest.TestCase):
    def test_defaults_from_empty_env(self):
        s = load_settings({})
        self.assertIsInstance(s, Settings)
        self.assertEqual(s.apply_model, "deepseek-v4-pro")
        self.assertEqual(s.agent_browser_max_output_chars, 20000)
        self.assertEqual(s.apply_max_turns, 60)
        self.assertEqual(s.self_improvement_diagnosis_max_turns, 20)
        self.assertEqual(s.max_rent, 1750.0)
        self.assertFalse(s.require_separate_bedroom)
        self.assertTrue(s.notify_enabled_flag)
        self.assertEqual(s.web_push_outcomes, frozenset({"submitted"}))
        self.assertIsNone(s.deepseek_api_key)

    def test_malformed_int_names_the_variable(self):
        with self.assertRaises(SettingsError) as ctx:
            load_settings({"APPLY_MAX_TURNS": "sixty"})
        self.assertIn("APPLY_MAX_TURNS", str(ctx.exception))

    def test_malformed_float_names_the_variable(self):
        with self.assertRaises(SettingsError) as ctx:
            load_settings({"MAX_RENT": "duizend"})
        self.assertIn("MAX_RENT", str(ctx.exception))

    def test_flag_convention_only_zero_disables(self):
        self.assertFalse(load_settings({"APPLY_FASTPATH_ENABLED": "0"}).apply_fastpath_enabled)
        self.assertTrue(load_settings({"APPLY_FASTPATH_ENABLED": "yes"}).apply_fastpath_enabled)

    def test_separate_bedroom_filter_flag(self):
        self.assertTrue(
            load_settings({"REQUIRE_SEPARATE_BEDROOM": "1"}).require_separate_bedroom)
        self.assertFalse(
            load_settings({"REQUIRE_SEPARATE_BEDROOM": "0"}).require_separate_bedroom)

    def test_playbook_model_falls_back_to_apply_model(self):
        s = load_settings({"APPLY_MODEL": "some-model"})
        self.assertEqual(s.playbook_model, "some-model")
        s = load_settings({"APPLY_MODEL": "some-model", "PLAYBOOK_MODEL": "other"})
        self.assertEqual(s.playbook_model, "other")

    def test_reasoning_effort_normalized(self):
        self.assertEqual(
            load_settings({"APPLY_REASONING_EFFORT": "MINIMAL"}).apply_reasoning_effort,
            "low")

    def test_csv_fields_strip_and_drop_empty(self):
        s = load_settings({"HEALTHCHECK_SERVICES": "orchestrator, dashboard ,,"})
        self.assertEqual(s.healthcheck_services, ("orchestrator", "dashboard"))

    def test_prune_keep_recent_floor(self):
        self.assertEqual(load_settings({"APPLY_PRUNE_KEEP_RECENT": "0"}).apply_prune_keep_recent, 1)

    def test_agent_browser_output_floor(self):
        self.assertEqual(
            load_settings({"AGENT_BROWSER_MAX_OUTPUT_CHARS": "10"}).agent_browser_max_output_chars,
            1000)

    def test_self_improvement_outcomes_csv(self):
        s = load_settings({"SELF_IMPROVEMENT_OUTCOMES": "blocked,error"})
        self.assertEqual(s.self_improvement_outcomes, frozenset({"blocked", "error"}))


if __name__ == "__main__":
    unittest.main()
