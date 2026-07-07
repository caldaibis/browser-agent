from __future__ import annotations

import base64
import unittest
from unittest.mock import patch

from src import gmail_watch
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


class _FakeListRequest:
    def __init__(self, ids):
        self._ids = ids

    def execute(self):
        return {"messages": [{"id": i} for i in self._ids]}


class _FakeGetRequest:
    def __init__(self, msg):
        self._msg = msg

    def execute(self):
        return self._msg


class _FakeMessagesMulti:
    def __init__(self, ids_by_provider, messages_by_id):
        self._ids_by_provider = ids_by_provider
        self._messages_by_id = messages_by_id

    def list(self, **kw):
        query = kw.get("q", "")
        provider = "stekkies" if "stekkies" in query else "huurwoningen"
        return _FakeListRequest(self._ids_by_provider.get(provider, []))

    def get(self, **kw):
        return _FakeGetRequest(self._messages_by_id[kw["id"]])


class _FakeUsersMulti:
    def __init__(self, ids_by_provider, messages_by_id):
        self._ids_by_provider = ids_by_provider
        self._messages_by_id = messages_by_id

    def messages(self):
        return _FakeMessagesMulti(self._ids_by_provider, self._messages_by_id)


class _FakeServiceMulti:
    def __init__(self, ids_by_provider, messages_by_id):
        self._ids_by_provider = ids_by_provider
        self._messages_by_id = messages_by_id

    def users(self):
        return _FakeUsersMulti(self._ids_by_provider, self._messages_by_id)


def _msg(subject: str, body: str, internal_date: str = "1782912000000") -> dict:
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {
        "internalDate": internal_date,
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

    def test_watch_events_batches_both_providers_newest_first(self):
        old_stekkies = _msg(
            "Your new Stekkies for you",
            "https://www.stekkies.com/en/api/v1/redirect/abc123",
            internal_date="1782912000000",
        )
        new_huurwoningen = _msg(
            "Net geplaatst: € 1.200 per maand, Freshstraat in Utrecht",
            "https://www.huurwoningen.nl/huren/utrecht/fresh/freshstraat/",
            internal_date="1782912060000",
        )
        svc = _FakeServiceMulti(
            {"stekkies": ["old"], "huurwoningen": ["new"]},
            {"old": old_stekkies, "new": new_huurwoningen},
        )
        with patch.object(gmail_watch, "get_service", return_value=svc):
            gen = gmail_watch.watch_events(poll_seconds=0)
            first = next(gen)
            second = next(gen)
        self.assertEqual(first.msg_id, "new")
        self.assertEqual(second.msg_id, "old")


if __name__ == "__main__":
    unittest.main()
