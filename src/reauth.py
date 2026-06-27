"""Re-authorize Gmail and write a fresh token with the current SCOPES.

Headless-friendly: prints the consent URL instead of opening a browser. Open the
URL in ANY browser (e.g. Windows Chrome); the localhost:8765 redirect still
reaches this process. Run after changing gmail_watch.SCOPES.

  python -m src.reauth
"""
from google_auth_oauthlib.flow import InstalledAppFlow

from .gmail_watch import SCOPES, CLIENT_SECRET, TOKEN


def main() -> None:
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
    creds = flow.run_local_server(
        port=8765,
        open_browser=False,
        authorization_prompt_message="AUTH URL >>> {url}",
    )
    TOKEN.write_text(creds.to_json(), encoding="utf-8")
    print("REAUTH_DONE: token written to", TOKEN)


if __name__ == "__main__":
    main()
