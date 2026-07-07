from __future__ import annotations

import unittest

from src.poller import filters
from src.poller.models import RawListing


def _listing(**kw) -> RawListing:
    base = dict(
        source_url="https://example.test/huren/utrecht/abcd1234/teststraat/",
        city="Utrecht",
        address="Teststraat 1",
        price=1400.0,
        surface=50.0,
        listing_type="apartment",
        title="Appartement Teststraat",
    )
    base.update(kw)
    return RawListing(**base)


class TestHardExclusion(unittest.TestCase):
    def test_students_only_dutch(self):
        # Verbatim from the huurportaal listing that burned a full agent run.
        reason = filters.hard_exclusion(
            "Beschrijving ALLEEN BESCHIKBAAR VOOR STUDENTEN VOOR MAXIMAAL 8 "
            "MAANDEN. Mooie woning.")
        self.assertIsNotNone(reason)
        self.assertIn("students-only", reason)

    def test_student_from_outside_town(self):
        reason = filters.hard_exclusion(
            "11 MAANDEN TE HUUR TOT VOOR EEN STUDENT VAN BUITEN UTRECHT, "
            "REACTIES ALLEEN ONLINE AUB")
        self.assertIsNotNone(reason)

    def test_students_excluded_is_fine(self):
        # The OPPOSITE case: students not welcome — must NOT veto.
        self.assertIsNone(filters.hard_exclusion(
            "Studenten en woningdelers behoren niet tot onze doelgroep."))

    def test_no_students_negation_is_fine(self):
        self.assertIsNone(filters.hard_exclusion(
            "Geen studenten. Alleen werkenden met vast contract."))

    def test_seniors_only(self):
        self.assertIsNotNone(filters.hard_exclusion(
            "Dit appartement in een seniorencomplex is uitsluitend voor "
            "senioren beschikbaar."))

    def test_short_stay(self):
        self.assertIsNotNone(filters.hard_exclusion(
            "Short stay appartement, volledig gemeubileerd."))

    def test_short_max_duration(self):
        self.assertIsNotNone(filters.hard_exclusion(
            "Te huur voor maximaal 4 maanden."))

    def test_long_max_duration_ok(self):
        self.assertIsNone(filters.hard_exclusion(
            "Huurcontract voor maximaal 24 maanden."))

    def test_normal_listing_ok(self):
        self.assertIsNone(filters.hard_exclusion(
            "Ruim licht appartement met balkon op het zuiden, nabij het "
            "centrum van Utrecht. Beschikbaar per direct."))

    def test_empty_ok(self):
        self.assertIsNone(filters.hard_exclusion(""))


class TestPassesWithDescription(unittest.TestCase):
    def test_vetoes_students_only_description(self):
        ok, reason = filters.passes(_listing(
            description="ALLEEN BESCHIKBAAR VOOR STUDENTEN."))
        self.assertFalse(ok)
        self.assertIn("students-only", reason)

    def test_passes_normal_description(self):
        ok, _ = filters.passes(_listing(
            description="Mooi appartement in Utrecht met eigen keuken."))
        self.assertTrue(ok)


if __name__ == "__main__":
    unittest.main()
