from __future__ import annotations

import os
import unittest
from unittest import mock

from src.bedroom_policy import BedroomLayout, classify_layout, disallowed_reason


class TestBedroomPolicy(unittest.TestCase):
    def test_studio_is_single_room(self):
        self.assertEqual(classify_layout("Lichte studio in Utrecht"), BedroomLayout.SINGLE_ROOM)

    def test_one_room_and_combined_room_are_single(self):
        for text in (
            "1-kamerappartement, alles in één ruimte",
            "Woon-/slaapkamer met keuken",
            "Living and sleeping area, bathroom separate",
            "Sleeping nook in the living room",
            "Studio with a slaapvide",
            "Sleeping loft above the living area",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_layout(text), BedroomLayout.SINGLE_ROOM)

    def test_explicit_separate_bedroom_overrides_studio_label(self):
        self.assertEqual(
            classify_layout("Studio met aparte slaapkamer"), BedroomLayout.SEPARATE)
        self.assertEqual(
            classify_layout("Studio with one separate bedroom"), BedroomLayout.SEPARATE)
        self.assertEqual(
            classify_layout("Studio with a separate sleeping area"), BedroomLayout.SEPARATE)

    def test_bedroom_and_two_rooms_are_separate(self):
        for text in (
            "Appartement met 1 slaapkamer",
            "2-kamerwoning met woonkamer en slaapkamer",
            "Woonkamer en slaapkamer",
            "Two-room apartment",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_layout(text), BedroomLayout.SEPARATE)

    def test_ambiguous_or_missing_text_passes(self):
        self.assertEqual(classify_layout("Ruim appartement nabij het centrum"), BedroomLayout.UNKNOWN)
        self.assertEqual(classify_layout(""), BedroomLayout.UNKNOWN)
        self.assertIsNone(disallowed_reason("Ruim appartement nabij het centrum"))

    def test_reason_contains_evidence(self):
        self.assertIn("studio", disallowed_reason("Lichte studio in Utrecht").lower())

    def test_enabled_filter_is_injected_into_live_agent_prompt(self):
        from src.prompts.apply_prompt import build_prompt

        with mock.patch.dict(os.environ, {"REQUIRE_SEPARATE_BEDROOM": "1"}):
            prompt = build_prompt({
                "source_url": "https://example.test/listing",
                "description": "Ruim appartement",
            })
        self.assertIn("SEPARATE BEDROOM (enabled)", prompt)


if __name__ == "__main__":
    unittest.main()
