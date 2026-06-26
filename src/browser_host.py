"""Always-on browser host.

Launches ONE persistent Chromium (our profile) with a CDP debugging port. Both
the Stekkies extractor and the Hermes apply agent attach to it over CDP, so all
sessions — Google (for "Sign in with Google" SSO), Stekkies, and every rental
site — live in a single profile you log into once.

Usage:
  python -m src.browser_host            # start the host (headed) and keep alive
  python -m src.browser_host --login    # start headed + open the sites to log into

Leave it running (e.g. in its own terminal, or as a service). The orchestrator
and apply stage attach to CONFIG.CDP_URL automatically.
"""
import sys
import time

from playwright.sync_api import sync_playwright

from .config import USER_DATA_DIR, CDP_PORT, CDP_URL

# Sites to open for the one-time interactive login pass (--login).
LOGIN_SITES = [
    "https://accounts.google.com/",            # Google session -> enables SSO
    "https://www.stekkies.com/en/profiles/login/",
    "https://ikwilhuren.nu/account/login",
    "https://www.pararius.nl/inloggen",
    "https://www.funda.nl/",                    # uses Google SSO
]


def main() -> int:
    do_login = "--login" in sys.argv
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            # Remove automation fingerprints so Google's "this browser may not
            # be secure" check passes. --disable-blink-features=AutomationControlled
            # drops navigator.webdriver; ignoring --enable-automation removes the
            # "controlled by automation" banner/flag.
            ignore_default_args=["--enable-automation"],
            args=[
                f"--remote-debugging-port={CDP_PORT}",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
                "--restore-last-session",
            ],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("about:blank")
        print(f"[browser-host] running. CDP endpoint: {CDP_URL}")
        if do_login:
            print("[browser-host] opening sites for one-time login. "
                  "Log into each (Google first), then leave this browser running.")
            for url in LOGIN_SITES:
                try:
                    (ctx.new_page()).goto(url, wait_until="domcontentloaded")
                except Exception as e:
                    print(f"  - could not open {url}: {e}")
        print("[browser-host] keep this process alive. Ctrl-C to stop.")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\n[browser-host] shutting down.")
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
