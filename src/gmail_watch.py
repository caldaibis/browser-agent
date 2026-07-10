"""Gmail trigger: detect new Stekkies 'new listing' emails fast and yield the
Stekkies listing URL found inside.

Auth: OAuth desktop flow. Put your OAuth client file at
  state/gmail_client_secret.json   (Google Cloud Console > APIs & Services >
  Credentials > OAuth client ID > Desktop app, in your own Google Cloud
  project). First run opens a browser to authorize; the token is cached in
  state/gmail_token.json.

Detection: low-latency polling (default 5s) of unread Stekkies mails. This is
simple and robust on a local/WSL machine. (Gmail push via Pub/Sub is lower
latency but needs a public webhook endpoint; see README.)

Run standalone (prints new listing URLs as they arrive):
  python -m src.gmail_watch
"""
import base64
import html
import quopri
import re
import time
from dataclasses import dataclass
from datetime import datetime
from collections.abc import Iterator

import httpx
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import PROJECT_ROOT
from .eventlog import get_logger

_LOG = get_logger("gmail")

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]
CLIENT_SECRET = PROJECT_ROOT / "state" / "gmail_client_secret.json"
TOKEN = PROJECT_ROOT / "state" / "gmail_token.json"

# Match listing mails. Huurwoningen confirmation mails ("Je reactie ... is
# verstuurd") are intentionally excluded; only new-listing alerts are actionable.
STEKKIES_QUERY = 'is:unread from:help@stekkies.com subject:"new Stekkies for you"'
HUURWONINGEN_QUERY = 'is:unread from:huurwoningen subject:"Net geplaatst"'
GMAIL_QUERY = STEKKIES_QUERY
POLL_SECONDS = 5

# Direct listing link in the plain-text body, e.g.
#   http://www.stekkies.com/en/api/v1/redirect/e70587cc...?utm_...
# ID is a hex hash (not digits); scheme may be http or https. We restrict to
# www.stekkies.com to skip the email.stekkies.com click-tracking wrappers.
LINK_RE = re.compile(r"https?://www\.stekkies\.com/[^\s\"'>]*?redirect/[A-Za-z0-9]+[^\s\"'>]*")
HREF_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.I)
HUURWONINGEN_SUBJECT_RE = re.compile(
    r"Net geplaatst:\s*(?P<price>€\s?[\d.,]+.*?)?,\s*(?P<address>.+)$",
    re.I,
)


@dataclass(frozen=True)
class GmailListingEvent:
    msg_id: str
    provider: str
    url: str | None
    received_ts: str = ""
    subject: str = ""
    address: str = ""
    price: str = ""

    @property
    def trigger(self) -> str:
        return {
            "stekkies": "stekkies_mail",
            "huurwoningen": "huurwoningen_mail",
        }.get(self.provider, f"{self.provider}_mail")


def get_service():
    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                raise SystemExit(
                    f"Missing OAuth client file: {CLIENT_SECRET}\n"
                    "Create a Desktop OAuth client in Google Cloud and save it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN.write_text(creds.to_json(), encoding="utf-8")
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _cte(part) -> str:
    for h in part.get("headers", []) or []:
        if h.get("name", "").lower() == "content-transfer-encoding":
            return h.get("value", "").lower()
    return ""


def _body_text(payload) -> str:
    out = []
    stack = [payload]
    while stack:
        part = stack.pop()
        data = part.get("body", {}).get("data")
        if data:
            raw = base64.urlsafe_b64decode(data)
            # Gmail returns the part still in its transfer encoding; undo
            # quoted-printable so soft line-breaks (=\n) and =3D etc. don't
            # mangle URLs.
            if _cte(part) == "quoted-printable":
                raw = quopri.decodestring(raw)
            out.append(raw.decode("utf-8", "replace"))
        stack.extend(part.get("parts", []) or [])
    return "\n".join(out)


def _headers(payload) -> dict[str, str]:
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", []) or []
    }


def _received_ts(msg: dict) -> str:
    internal = msg.get("internalDate")
    if internal is None:
        return ""
    try:
        return datetime.fromtimestamp(int(internal) / 1000).isoformat(timespec="seconds")
    except Exception:
        return ""


def extract_listing_url(svc, msg_id: str) -> str | None:
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    text = _body_text(msg["payload"])
    m = LINK_RE.search(text)
    return m.group(0) if m else None


def message_received_ts(msg_id: str | None) -> str:
    if not msg_id:
        return ""
    try:
        svc = get_service()
        msg = svc.users().messages().get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["Date"],
        ).execute()
        return _received_ts(msg)
    except Exception:
        return ""


def _resolve_redirect(url: str, timeout: float = 8.0) -> str:
    """Follow a click-tracking link to its final listing URL.

    Without this, Huurwoningen mail events keep the track.huurwoningen.nl
    wrapper as their source_url, which never canonically matches the direct
    URL the poller sees for the same listing -- silently breaking dedup and
    the dashboard's mail-race stats. Fail open (keep the tracking URL) so a
    network hiccup never blocks the mail trigger.
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout) as client:
            resp = client.get(url)
            return str(resp.url)
    except Exception:
        return url


def _message_event(svc, msg_id: str, provider: str) -> dict:
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    payload = msg.get("payload", {})
    headers = _headers(payload)
    text = _body_text(payload)
    stekkies_url = ""
    source_url = ""
    if provider == "stekkies":
        m = LINK_RE.search(text)
        stekkies_url = m.group(0) if m else ""
    elif provider == "huurwoningen":
        anchors = _anchor_links(text)
        links = _links_from_text(text)
        listing_links = [
            u for u, _label in anchors
            if "huurwoningen.nl" in u and (
                "/huren/" in u or "/huurwoning" in u or "/woning/" in u
            )
        ]
        listing_links.extend([
            u for u in links
            if u not in listing_links and "huurwoningen.nl" in u and (
                "/huren/" in u or "/huurwoning" in u or "/woning/" in u
            )
        ])
        click_links = _prioritized_huurwoningen_clicks(anchors)
        click_links.extend([
            u for u in links
            if "track.huurwoningen.nl/ls/click" in u and u not in click_links
        ])
        source_url = listing_links[0] if listing_links else (click_links[0] if click_links else "")
        if source_url and "track.huurwoningen.nl" in source_url:
            source_url = _resolve_redirect(source_url)
    return {
        "provider": provider,
        "msg_id": msg_id,
        "received_ts": _received_ts(msg),
        "from": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "stekkies_url": stekkies_url,
        "source_url": source_url,
    }


def _anchor_links(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for href, label in re.findall(
        r"<a[^>]+href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        text or "",
        re.I | re.S,
    ):
        url = html.unescape(href).rstrip("\").,;")
        clean_label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", label)).strip()
        out.append((url, clean_label))
    return out


def _links_from_text(text: str) -> list[str]:
    """Extract href and plain-text URLs from an email body, preserving SendGrid
    click links that may be the only actionable Huurwoningen listing URL."""
    raw = HREF_RE.findall(text or "")
    raw.extend(re.findall(r"https?://[^\s\"'<>]+", text or ""))
    out: list[str] = []
    seen: set[str] = set()
    for u in raw:
        clean = html.unescape(u).rstrip("\").,;")
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def _prioritized_huurwoningen_clicks(anchors: list[tuple[str, str]]) -> list[str]:
    clicks = [
        (url, label.lower())
        for url, label in anchors
        if "track.huurwoningen.nl/ls/click" in url
    ]
    wanted = ("bekijk", "woning", "reageer", "meer informatie", "details")
    ranked = [url for url, label in clicks if any(word in label for word in wanted)]
    ranked.extend(url for url, _label in clicks if url not in ranked)
    return ranked


def _huurwoningen_metadata(subject: str) -> tuple[str, str]:
    m = HUURWONINGEN_SUBJECT_RE.search(subject or "")
    if not m:
        return (subject or "").strip(), "?"
    return (m.group("address") or "").strip(), (m.group("price") or "?").strip()


def _event_from_message(svc, msg_id: str, provider: str) -> GmailListingEvent:
    ev = _message_event(svc, msg_id, provider)
    if provider == "stekkies":
        return GmailListingEvent(
            msg_id=msg_id,
            provider=provider,
            url=ev.get("stekkies_url") or None,
            received_ts=ev.get("received_ts", ""),
            subject=ev.get("subject", ""),
        )
    if provider == "huurwoningen":
        address, price = _huurwoningen_metadata(ev.get("subject", ""))
        return GmailListingEvent(
            msg_id=msg_id,
            provider=provider,
            url=ev.get("source_url") or None,
            received_ts=ev.get("received_ts", ""),
            subject=ev.get("subject", ""),
            address=address,
            price=price,
        )
    return GmailListingEvent(
        msg_id=msg_id,
        provider=provider,
        url=ev.get("source_url") or ev.get("stekkies_url") or None,
        received_ts=ev.get("received_ts", ""),
        subject=ev.get("subject", ""),
    )


def recent_mail_events(days: int = 30, max_results: int = 100) -> list[dict]:
    """Return recent Stekkies and Huurwoningen mail signals for dashboard timing.

    Stekkies mails only expose the Stekkies redirect URL; their external source
    URL is filled later by correlating with processed mail_summary records.
    Huurwoningen mails usually include the source listing URL directly.
    """
    svc = get_service()
    queries = [
        ("stekkies", f'from:help@stekkies.com subject:"new Stekkies for you" newer_than:{days}d'),
        ("huurwoningen", f"from:huurwoningen newer_than:{days}d"),
    ]
    events: list[dict] = []
    seen: set[str] = set()
    for provider, query in queries:
        try:
            resp = svc.users().messages().list(
                userId="me", q=query, maxResults=max_results,
            ).execute()
        except Exception:
            continue
        for m in resp.get("messages", []) or []:
            msg_id = m.get("id", "")
            if not msg_id or msg_id in seen:
                continue
            seen.add(msg_id)
            try:
                events.append(_message_event(svc, msg_id, provider))
            except Exception:
                continue
    events.sort(key=lambda e: e.get("received_ts") or "", reverse=True)
    return events


def mark_read(msg_id: str) -> None:
    svc = get_service()
    svc.users().messages().modify(
        userId="me", id=msg_id, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


def watch_events(poll_seconds: int = POLL_SECONDS) -> Iterator[GmailListingEvent]:
    """Yield actionable unread listing mails from Stekkies and Huurwoningen."""
    svc = get_service()
    queries = [
        ("stekkies", STEKKIES_QUERY),
        ("huurwoningen", HUURWONINGEN_QUERY),
    ]
    _LOG.info("watching listing mail: " + " | ".join(q for _p, q in queries)
          + f" every {poll_seconds}s...")
    seen_this_process: set[str] = set()
    while True:
        try:
            # Gather this cycle's new mails from BOTH providers, then yield
            # NEWEST-LISTING-FIRST. Rentals are won in the first minute, and
            # the applier is serial (one shared browser), so on a burst the
            # order of this queue decides which listings are still winnable by
            # the time we reach them. Verified worth it 07-07-2026: 13 mails
            # arrived in a 32-min window; FIFO made the last-queued (but often
            # freshest) listing wait ~25 min behind older ones.
            batch: list[GmailListingEvent] = []
            for provider, query in queries:
                resp = svc.users().messages().list(
                    userId="me", q=query, maxResults=10,
                ).execute()
                for m in resp.get("messages", []) or []:
                    msg_id = m.get("id", "")
                    if not msg_id or msg_id in seen_this_process:
                        continue
                    seen_this_process.add(msg_id)
                    batch.append(_event_from_message(svc, msg_id, provider))
            # Newest received first; empty received_ts sorts last.
            batch.sort(key=lambda e: e.received_ts or "", reverse=True)
            for ev in batch:
                if not ev.url:
                    _LOG.info(f"{ev.provider} mail {ev.msg_id} had no listing link; skipped.")
                yield ev
        except Exception as e:  # keep the watcher alive
            _LOG.info(f"error: {e}")
        time.sleep(poll_seconds)


def watch(poll_seconds: int = POLL_SECONDS) -> Iterator[tuple[str, str | None]]:
    """Backward-compatible Stekkies-only iterator."""
    svc = get_service()
    _LOG.info(f"watching (query='{STEKKIES_QUERY}', every {poll_seconds}s)...")
    while True:
        try:
            resp = svc.users().messages().list(
                userId="me", q=STEKKIES_QUERY, maxResults=10,
            ).execute()
            for m in resp.get("messages", []) or []:
                msg_id = m.get("id", "")
                if not msg_id:
                    continue
                ev = _event_from_message(svc, msg_id, "stekkies")
                if ev.url:
                    yield ev.msg_id, ev.url
                else:
                    _LOG.info(f"mail {msg_id} had no listing link; skipped.")
                    yield ev.msg_id, None
        except Exception as e:  # keep the watcher alive
            _LOG.info(f"error: {e}")
        time.sleep(poll_seconds)


def main() -> None:
    for ev in watch_events():
        _LOG.info(f"NEW {ev.provider} LISTING {ev.msg_id}: {ev.url}")


if __name__ == "__main__":
    main()
