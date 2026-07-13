from __future__ import annotations

import unittest
from dataclasses import replace
from unittest.mock import patch

from src.applicant_profile import PROFILE
from src import message_template
from src.message_template import REFERENCE_APPLICATION_MESSAGE


class TestReferenceApplicationMessage(unittest.TestCase):
    def test_generated_from_profile_identity(self):
        self.assertIn(PROFILE.name, REFERENCE_APPLICATION_MESSAGE)
        self.assertIn(PROFILE.email, REFERENCE_APPLICATION_MESSAGE)
        self.assertIn(PROFILE.phone, REFERENCE_APPLICATION_MESSAGE)

    def test_no_placeholder_identity(self):
        self.assertNotIn("Jane Doe", REFERENCE_APPLICATION_MESSAGE)
        self.assertNotIn("you@example.com", REFERENCE_APPLICATION_MESSAGE)

    def test_employment_wording_is_language_specific(self):
        profile = replace(
            PROFILE,
            # Reproduce the old production value that contaminated Dutch copy.
            employment="permanent contract (vast contract), scientist at Example Research",
            employment_nl="loondienst met een vast contract als wetenschapper bij Example Research",
            employment_en="permanent employment as a scientist at Example Research",
        )
        with patch.object(message_template, "PROFILE", profile):
            text = message_template.build_reference_application_message()
        dutch, english = text.split("-------------", maxsplit=1)
        self.assertIn("wetenschapper bij Example Research", dutch)
        self.assertNotIn("scientist at Example Research", dutch)
        self.assertIn("scientist at Example Research", english)
        self.assertNotIn("wetenschapper bij Example Research", english)


if __name__ == "__main__":
    unittest.main()
