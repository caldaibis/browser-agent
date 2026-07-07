from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from src import listing_context

_DETAIL_HTML = """
<html><head>
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "RealEstateListing",
 "name": "Appartement Teststraat 1",
 "description": "Ruim appartement in Utrecht. ALLEEN VOOR STUDENTEN.",
 "url": "https://huurportaal.nl/listings/teststraat-1-p123",
 "offers": {"@type": "Offer", "price": "1450"}}
</script>
</head><body>ok</body></html>
"""

_PRICE_ONLY_HTML = """
<html><body>
<section>
  <p>Parkeerplaats optioneel: € 90,- /mnd</p>
  <strong>Huurprijs € 1.355,- /mnd</strong>
</section>
</body></html>
"""


class TestFetchContext(unittest.TestCase):
    def test_parses_description_and_price(self):
        resp = Mock(status_code=200, text=_DETAIL_HTML)
        with patch.object(listing_context.httpx, "get", return_value=resp):
            ctx = listing_context.fetch_context(
                "https://huurportaal.nl/listings/teststraat-1-p123")
        self.assertIsNotNone(ctx)
        self.assertIn("ALLEEN VOOR STUDENTEN", ctx.description)
        self.assertEqual(ctx.price, 1450.0)

    def test_non_200_is_none(self):
        resp = Mock(status_code=403, text="")
        with patch.object(listing_context.httpx, "get", return_value=resp):
            self.assertIsNone(listing_context.fetch_context("https://x.test/a"))

    def test_network_error_is_none(self):
        with patch.object(listing_context.httpx, "get",
                          side_effect=OSError("boom")):
            self.assertIsNone(listing_context.fetch_context("https://x.test/a"))

    def test_no_usable_content_is_none(self):
        resp = Mock(status_code=200, text="<html><body>hi</body></html>")
        with patch.object(listing_context.httpx, "get", return_value=resp):
            self.assertIsNone(listing_context.fetch_context("https://x.test/a"))

    def test_html_monthly_price_fallback_without_jsonld(self):
        resp = Mock(status_code=200, text=_PRICE_ONLY_HTML)
        with patch.object(listing_context.httpx, "get", return_value=resp):
            ctx = listing_context.fetch_context("https://ikwilhuren.nu/object/test/")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx.price, 1355.0)
        self.assertIsNone(ctx.surface)


class TestIsAggregator(unittest.TestCase):
    def test_known_aggregators(self):
        self.assertTrue(listing_context.is_aggregator(
            "https://huurportaal.nl/listings/x-p1"))
        self.assertTrue(listing_context.is_aggregator(
            "https://www.huurwoningen.nl/huren/utrecht/ab12cd34/straat/"))

    def test_direct_site(self):
        self.assertFalse(listing_context.is_aggregator(
            "https://www.rebogroep.nl/nl/aanbod/x"))


if __name__ == "__main__":
    unittest.main()
