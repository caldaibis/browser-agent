from __future__ import annotations

import unittest
from unittest.mock import patch

from src import apply
from src.models import Listing
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

    def test_apply_short_circuits_known_paid_application_site(self):
        result = apply.apply({
            "source_url": "https://your-house.nl/woningaanbod/huur/utrecht/test/1",
            "source_name": "your-house.nl",
            "address": "Teststraat 1",
            "price": "€ 1.500 per maand",
        })
        self.assertEqual(result.outcome, "payment_required")
        self.assertEqual(result.rc, 0)
        self.assertIn("requires payment", result.summary)

    def test_payment_wording_detected_without_browser(self):
        reason = apply._payment_required_reason(Listing.from_json({
            "source_url": "https://example.test/listing",
            "description": "Om te reageren is een lidmaatschap van €25 per jaar vereist.",
        }))
        self.assertIsNotNone(reason)

    def test_free_registration_wording_not_detected_as_payment(self):
        reason = apply._payment_required_reason(Listing.from_json({
            "source_url": "https://example.test/listing",
            "description": "Geen inschrijfkosten. Reageren is gratis.",
        }))
        self.assertIsNone(reason)


if __name__ == "__main__":
    unittest.main()
