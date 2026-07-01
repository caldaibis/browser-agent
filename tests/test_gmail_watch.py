from __future__ import annotations

import base64
import unittest

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


if __name__ == "__main__":
    unittest.main()
