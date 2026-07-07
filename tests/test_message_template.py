from __future__ import annotations

import unittest

from src.applicant_profile import PROFILE
from src.message_template import REFERENCE_APPLICATION_MESSAGE


class TestReferenceApplicationMessage(unittest.TestCase):
    def test_generated_from_profile_identity(self):
        self.assertIn(PROFILE.name, REFERENCE_APPLICATION_MESSAGE)
        self.assertIn(PROFILE.email, REFERENCE_APPLICATION_MESSAGE)
        self.assertIn(PROFILE.phone, REFERENCE_APPLICATION_MESSAGE)

    def test_no_placeholder_identity(self):
        self.assertNotIn("Jane Doe", REFERENCE_APPLICATION_MESSAGE)
        self.assertNotIn("you@example.com", REFERENCE_APPLICATION_MESSAGE)


if __name__ == "__main__":
    unittest.main()
