from __future__ import annotations

import unittest
from unittest import mock

from src import apply
from src.models import Listing
from src.rent_policy import parse_rent


class TestRentPolicy(unittest.TestCase):
    def test_parse_rent(self):
        self.assertEqual(parse_rent("€ 1.750,00 per maand"), 1750)
        self.assertEqual(parse_rent("EUR 3500 p/m"), 3500)
        self.assertIsNone(parse_rent("?"))

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

    def test_apply_short_circuits_single_room_when_filter_enabled(self):
        old = apply.REQUIRE_SEPARATE_BEDROOM
        apply.REQUIRE_SEPARATE_BEDROOM = True
        try:
            result = apply.apply({
                "source_url": "https://example.test/listing",
                "source_name": "Example",
                "address": "Teststraat 1",
                "title": "Studio in Utrecht",
                "description": "Studio, alles in één ruimte.",
                "price": "€ 1.500 per maand",
            })
        finally:
            apply.REQUIRE_SEPARATE_BEDROOM = old
        self.assertEqual(result.outcome, "not_eligible")
        self.assertEqual(result.rc, 0)
        self.assertIn("separate bedroom is required", result.summary)

    def test_apply_bedroom_filter_is_fail_open_for_ambiguous_text(self):
        old = apply.REQUIRE_SEPARATE_BEDROOM
        apply.REQUIRE_SEPARATE_BEDROOM = True
        try:
            with mock.patch.object(
                apply, "run_agent",
                return_value=apply.AgentResult(rc=0, outcome="blocked", summary="test"),
            ):
                with mock.patch.object(apply, "try_fast_apply", return_value=None):
                    result = apply.apply({
                        "source_url": "https://example.test/listing",
                        "source_name": "Example",
                        "address": "Teststraat 1",
                        "description": "Ruim appartement nabij het centrum.",
                        "price": "€ 1.500 per maand",
                    })
        finally:
            apply.REQUIRE_SEPARATE_BEDROOM = old
        self.assertEqual(result.outcome, "blocked")

    def test_bedroom_filter_does_not_classify_agency_name(self):
        with mock.patch.object(apply, "fetch_context", return_value=None):
            reason = apply._separate_bedroom_required_reason(Listing.from_json({
                "source_url": "https://example.test/listing",
                "source_name": "Studio Wonen",
                "title": "Appartement nabij het centrum",
            }))
        self.assertIsNone(reason)

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
