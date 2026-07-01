from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from src.gmail_watch import _event_from_message, _huurwoningen_metadata


class _FakeMessages:
    def __init__(self, msg):
        self._msg = msg

    def get(self, **_kw):
        return self

    def execute(self):
        return self._msg


class _FakeUsers:
    def __init__(self, msg):
        self._msg = msg

    def messages(self):
        return _FakeMessages(self._msg)


class _FakeService:
    def __init__(self, msg):
        self._msg = msg

    def users(self):
        return _FakeUsers(self._msg)


def _msg(subject: str, body: str) -> dict:
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "internalDate": "1782912000000",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": "Huurwoningen <mail@huurwoningen.nl>"},
            ],
            "body": {"data": data},
        },
    }


class TestHuurwoningenMail(unittest.TestCase):
    def test_subject_metadata(self):
        address, price = _huurwoningen_metadata(
            "Net geplaatst: € 1.600 per maand, Joseph Haydnlaan in Utrecht"
        )
        self.assertEqual(address, "Joseph Haydnlaan in Utrecht")
        self.assertEqual(price, "€ 1.600 per maand")

    def test_event_extracts_direct_listing_url(self):
        msg = _msg(
            "Net geplaatst: € 1.015 per maand, Hof van Oslo in Utrecht",
            "Bekijk: https://www.huurwoningen.nl/huren/utrecht/abc123/hof-van-oslo/",
        )
        ev = _event_from_message(_FakeService(msg), "msg-1", "huurwoningen")
        self.assertEqual(ev.provider, "huurwoningen")
        self.assertEqual(ev.trigger, "huurwoningen_mail")
        self.assertEqual(ev.url, "https://www.huurwoningen.nl/huren/utrecht/abc123/hof-van-oslo/")
        self.assertEqual(ev.address, "Hof van Oslo in Utrecht")

    def test_event_resolves_tracking_link_when_direct_url_is_hidden(self):
        msg = _msg(
            "Net geplaatst: € 1.304 per maand, Van Sijpesteijnkade in Utrecht",
            '<a href="http://track.huurwoningen.nl/ls/click?upn=u001.image"> </a>'
            '<a href="http://track.huurwoningen.nl/ls/click?upn=u001.cta">Bekijk woning</a>',
        )
        resolved = "https://www.huurwoningen.nl/huren/utrecht/def456/van-sijpesteijnkade/"
        with patch("src.gmail_watch._resolve_redirect", return_value=resolved) as mock_resolve:
            ev = _event_from_message(_FakeService(msg), "msg-2", "huurwoningen")
        mock_resolve.assert_called_once_with("http://track.huurwoningen.nl/ls/click?upn=u001.cta")
        self.assertEqual(ev.url, resolved)
        self.assertEqual(ev.price, "€ 1.304 per maand")

    def test_resolve_redirect_falls_back_to_original_url_on_failure(self):
        from src.gmail_watch import _resolve_redirect

        with patch("httpx.Client") as mock_client_cls:
            mock_client_cls.return_value.__enter__.side_effect = Exception("boom")
            url = "http://track.huurwoningen.nl/ls/click?upn=u001.cta"
            self.assertEqual(_resolve_redirect(url), url)


if __name__ == "__main__":
    unittest.main()
