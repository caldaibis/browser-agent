"""One-time interactive login.

Opens a visible Chromium with a PERSISTENT profile. You log into Stekkies
(and stay logged in). The session is saved in state/chromium-profile and reused
by the bot forever, so the hot path never has to sign in.

Run:  python -m src.login_setup
Then: log in, and press ENTER in this terminal when done.
"""
import sys
from playwright.sync_api import sync_playwright

from .config import USER_DATA_DIR, STEKKIES_LOGIN_URL


def main() -> None:
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(STEKKIES_LOGIN_URL, wait_until="domcontentloaded")
        print("\n=== Log into Stekkies in the browser window. ===")
        print("Auto-detecting login (waiting up to 10 min)...")

        # Consider login complete once the password field is gone AND we're no
        # longer on a /login/ URL. Poll so no terminal input is required.
        for _ in range(600):
            try:
                on_login_url = "/login" in page.url
                has_pw = page.locator("input[type=password]").count() > 0
                if not on_login_url and not has_pw:
                    print("Login detected at:", page.url)
                    break
            except Exception:
                pass
            page.wait_for_timeout(1000)
        else:
            print("Timed out waiting for login; closing anyway.")

        ctx.close()
        print("Session saved to:", USER_DATA_DIR)


if __name__ == "__main__":
    sys.exit(main())
