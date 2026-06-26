"""Gmail trigger: detect new Stekkies 'new listing' emails fast and yield the
Stekkies listing URL found inside.

Auth: OAuth desktop flow. Put your OAuth client file at
  state/gmail_client_secret.json   (Google Cloud Console > APIs & Services >
  Credentials > OAuth client ID > Desktop app, in project
  your-gcp-project). First run opens a browser to authorize; the
  token is cached in state/gmail_token.json.

Detection: low-latency polling (default 5s) of unread Stekkies mails. This is
simple and robust on a local/WSL machine. (Gmail push via Pub/Sub is lower
latency but needs a public webhook endpoint; see README.)

Run standalone (prints new listing URLs as they arrive):
  python -m src.gmail_watch
"""
import base64
import quopri
import re
import time
from pathlib import Path
from typing import Iterator

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

from .config import PROJECT_ROOT

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
CLIENT_SECRET = PROJECT_ROOT / "state" / "gmail_client_secret.json"
TOKEN = PROJECT_ROOT / "state" / "gmail_token.json"

# Match Stekkies "new match/listing" mails. Tune the query to your real subjects.
# Listing mails come from help@stekkies.com with a stable subject.
GMAIL_QUERY = 'is:unread from:help@stekkies.com subject:"new Stekkies for you"'
POLL_SECONDS = 5

# Direct listing link in the plain-text body, e.g.
#   http://www.stekkies.com/en/api/v1/redirect/e70587cc...?utm_...
# ID is a hex hash (not digits); scheme may be http or https. We restrict to
# www.stekkies.com to skip the email.stekkies.com click-tracking wrappers.
LINK_RE = re.compile(r"https?://www\.stekkies\.com/[^\s\"'>]*?redirect/[A-Za-z0-9]+[^\s\"'>]*")


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


def extract_listing_url(svc, msg_id: str) -> str | None:
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    text = _body_text(msg["payload"])
    m = LINK_RE.search(text)
    return m.group(0) if m else None


def watch(poll_seconds: int = POLL_SECONDS) -> Iterator[tuple[str, str]]:
    """Yield (message_id, listing_url) for each new Stekkies mail. Marks read."""
    svc = get_service()
    print(f"[gmail] watching (query='{GMAIL_QUERY}', every {poll_seconds}s)...")
    while True:
        try:
            resp = svc.users().messages().list(userId="me", q=GMAIL_QUERY, maxResults=10).execute()
            for m in resp.get("messages", []):
                url = extract_listing_url(svc, m["id"])
                # Mark read so we don't reprocess.
                svc.users().messages().modify(
                    userId="me", id=m["id"], body={"removeLabelIds": ["UNREAD"]}
                ).execute()
                if url:
                    yield m["id"], url
                else:
                    print(f"[gmail] mail {m['id']} had no listing link; skipped.")
        except Exception as e:  # keep the watcher alive
            print("[gmail] error:", e)
        time.sleep(poll_seconds)


def main() -> None:
    for msg_id, url in watch():
        print(f"[gmail] NEW LISTING {msg_id}: {url}")


if __name__ == "__main__":
    main()
