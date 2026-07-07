from __future__ import annotations

import unittest

from src.poller.models import SiteConfig
from src.poller.parsers import make_anchor_parser


class TestAnchorParserMetadata(unittest.TestCase):
    def test_extracts_nearby_monthly_price_and_surface(self):
        parser = make_anchor_parser(r"/appartement-te-huur/utrecht/[0-9a-f]+/")
        html = """
        <article>
          <a href="/appartement-te-huur/utrecht/abc123/">Bilderdijkstraat 25</a>
          <span>€ 1.450,- /mnd</span>
          <span>52 m²</span>
        </article>
        """
        listings = parser(html, SiteConfig(name="pararius.nl", list_url="https://www.pararius.nl/"))
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].price, 1450.0)
        self.assertEqual(listings[0].surface, 52.0)
        self.assertIn("Bilderdijkstraat", listings[0].title)

    def test_ignores_non_monthly_euro_amounts(self):
        parser = make_anchor_parser(r"/woning/[a-z0-9-]+")
        html = """
        <article>
          <a href="/woning/teststraat">Teststraat</a>
          <span>Borg € 2.900</span>
        </article>
        """
        listings = parser(html, SiteConfig(name="example.test", list_url="https://example.test/"))
        self.assertEqual(len(listings), 1)
        self.assertIsNone(listings[0].price)

    def test_extracts_price_with_text_before_pm_marker(self):
        parser = make_anchor_parser(r"/nl/huurwoning-utrecht/[a-z0-9-]+")
        html = """
        <article>
          <a href="/nl/huurwoning-utrecht/de-richmond/parijsboulevard-59-utrecht-9075">
            Parijsboulevard 59 Utrecht
          </a>
          <span>€ 1655,- huurprijs p.m.</span>
        </article>
        """
        listings = parser(html, SiteConfig(name="vesteda.com", list_url="https://www.vesteda.com/"))
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].price, 1655.0)


if __name__ == "__main__":
    unittest.main()
