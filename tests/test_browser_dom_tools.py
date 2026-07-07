from __future__ import annotations

import unittest

from src import browser_dom_tools


class TestConsentSyncUrls(unittest.TestCase):
    def test_pararius_user_sync_url_is_consent_sync(self):
        self.assertTrue(browser_dom_tools._is_consent_sync_url(
            "https://www.pararius.nl/user-sync?provider=x"))

    def test_common_adtech_sync_url_is_consent_sync(self):
        self.assertTrue(browser_dom_tools._is_consent_sync_url(
            "https://ib.adnxs.com/usersync?gdpr=1"))

    def test_normal_listing_url_is_not_consent_sync(self):
        self.assertFalse(browser_dom_tools._is_consent_sync_url(
            "https://www.pararius.nl/appartement-te-huur/utrecht/abc123/"))


if __name__ == "__main__":
    unittest.main()
