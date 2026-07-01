from __future__ import annotations

import unittest
from unittest.mock import patch

from src import apply
from src.poller import filters
from src.poller.models import RawListing
from src.rent_policy import parse_rent


class TestRentPolicy(unittest.TestCase):
    def test_parse_rent(self):
        self.assertEqual(parse_rent("€ 1.750,00 per maand"), 1750)
        self.assertEqual(parse_rent("EUR 3500 p/m"), 3500)
        self.assertIsNone(parse_rent("?"))

    def test_poller_rejects_unknown_price_by_default(self):
        listing = RawListing(
            source_url="https://www.pararius.nl/appartement-te-huur/utrecht/abc/",
            city="Utrecht",
        )
        with patch.object(filters, "REQUIRE_KNOWN_PRICE", True):
            ok, reason = filters.passes(listing)
        self.assertFalse(ok)
        self.assertIn("price unknown", reason)

    def test_poller_rejects_price_above_cap(self):
        listing = RawListing(
            source_url="https://www.pararius.nl/appartement-te-huur/utrecht/abc/",
            city="Utrecht",
            price=3500,
        )
        ok, reason = filters.passes(listing)
        self.assertFalse(ok)
        self.assertIn("> €1750", reason)

    def test_apply_short_circuits_price_above_cap(self):
        result = apply.apply({
            "source_url": "https://example.test/listing",
            "source_name": "Example",
            "address": "Teststraat 1",
            "price": "€ 3.500 per maand",
        })
        self.assertEqual(result.outcome, "not_eligible")
        self.assertEqual(result.rc, 0)
        self.assertIn("above the configured max rent", result.summary)


if __name__ == "__main__":
    unittest.main()
