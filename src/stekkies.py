"""Stekkies stage: open a listing (logged-in) and extract what we need to apply.

Returns a dict with:
  - listing_url    : the Stekkies listing page we landed on
  - source_url     : external "Go to listing" URL (where the real application is)
  - source_name    : e.g. "Ik Wil Huren"
  - letter         : the pre-written response/motivation letter (plain text)
  - title / price / address : listing metadata (best-effort)

Run standalone:  python -m src.stekkies "<stekkies_listing_or_redirect_url>"
"""
import html
import json
import re
import sys
from dataclasses import dataclass, asdict

from playwright.sync_api import sync_playwright


def _clean_html_text(raw: str) -> str:
    """Turn the letter's HTML into clean plain text."""
    s = re.sub(r"(?i)</p\s*>", "\n\n", raw)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n\s*\n+", "\n\n", s)
    return s.strip()

from .config import USER_DATA_DIR, LOG_DIR, SCREENSHOT_DIR, CDP_URL


@dataclass
class Listing:
    listing_url: str
    source_url: str
    source_name: str
    letter: str
    title: str
    price: str
    address: str


EXTRACT_JS = r"""
() => {
  const txt = (sel) => {
    const el = document.querySelector(sel);
    return el ? el.innerText.trim() : '';
  };
  // External "Go to listing" link: first non-Stekkies external object link.
  let source_url = '', source_name = '';
  const links = [...document.querySelectorAll('a[href^="http"]')];
  const ext = links.find(a => {
    const h = a.href;
    return !/stekkies\.com|google|gstatic|facebook|leafletjs|unpkg|visualwebsiteoptimizer/i.test(h);
  });
  if (ext) source_url = ext.href;

  // "Found on: <source>" label
  const foundOn = [...document.querySelectorAll('*')]
    .map(e => e.innerText || '')
    .find(t => /Found on:/i.test(t) && t.length < 60);
  if (foundOn) source_name = foundOn.replace(/.*Found on:\s*/i, '').trim();

  const letter = (document.querySelector('textarea[name="application_letter"]')
                  || document.querySelector('#message-content'))?.value || '';

  return {
    source_url,
    source_name,
    letter,
    title: txt('h1') || document.title,
    price: (document.body.innerText.match(/€\s?[\d.,]+/) || [''])[0],
    address: txt('h1'),
  };
}
"""


def _extract_from_page(page, url: str) -> Listing:
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(1500)
    data = page.evaluate(EXTRACT_JS)
    data["letter"] = _clean_html_text(data["letter"])
    data["source_name"] = data["source_name"].split("\n")[0].strip()
    # Address: pull "<street> <number>" out of the letter if present.
    m = re.search(r"woning aan de ([^,\n]+?)(?: tegen| tegenkwam|,|\n)", data["letter"])
    if m:
        data["address"] = m.group(1).strip()
    listing = Listing(listing_url=page.url, **data)
    page.screenshot(path=str(SCREENSHOT_DIR / "stekkies_listing.png"), full_page=True)
    return listing


def extract_listing(url: str, headless: bool = True) -> Listing:
    """Extract listing data. Prefers attaching to the always-on browser host
    (CDP_URL) so it shares the logged-in profile; falls back to launching its
    own persistent context if the host isn't running."""
    with sync_playwright() as p:
        # Try the shared CDP host first.
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                return _extract_from_page(page, url)
            finally:
                page.close()
                browser.close()  # detaches CDP; does NOT kill the host browser
        except Exception:
            pass  # host not up — fall back to our own profile

        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR), headless=headless,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            return _extract_from_page(page, url)
        finally:
            ctx.close()


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m src.stekkies <stekkies_url>")
        return 2
    listing = extract_listing(sys.argv[1], headless=True)
    out = LOG_DIR / "last_listing.json"
    out.write_text(json.dumps(asdict(listing), indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(asdict(listing), indent=2, ensure_ascii=False))
    print(f"\nSaved -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
