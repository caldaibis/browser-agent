"""Enumerate listing URLs from the Stekkies matches page (logged-in, over CDP).

Stekkies paginates matches with a JS pager (<nav aria-label="Page navigation">
containing clickable <li> page numbers), ~10 listings per page.

Usage:
  python -m src.matches [num_pages]     # default 2 pages, prints listing URLs
"""
import asyncio
import sys

from playwright.async_api import async_playwright

from .config import CDP_URL

MATCHES_URL = "https://www.stekkies.com/en/profiles/matches/"

_COLLECT_JS = """() => [...new Set(
  [...document.querySelectorAll('a[href]')].map(a => a.href)
   .filter(h => /\\/h\\/redirect\\/\\d+/.test(h))
)]"""


async def _collect(num_pages: int) -> list[str]:
    urls: list[str] = []
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(CDP_URL)
        ctx = b.contexts[0] if b.contexts else await b.new_context()
        pg = await ctx.new_page()
        try:
            await pg.goto(MATCHES_URL, wait_until="networkidle")
            await pg.wait_for_timeout(1500)
            for page_no in range(1, num_pages + 1):
                if page_no > 1:
                    # click the <li> page number inside the pager nav
                    clicked = await pg.evaluate(
                        """(n) => {
                          const nav = document.querySelector('nav[aria-label="Page navigation"]');
                          if (!nav) return false;
                          const li = [...nav.querySelectorAll('li')]
                            .find(e => e.innerText.trim() === String(n));
                          if (!li) return false;
                          (li.querySelector('a,button') || li).click();
                          return true;
                        }""", page_no)
                    if not clicked:
                        break
                    await pg.wait_for_timeout(2000)
                page_urls = await pg.evaluate(_COLLECT_JS)
                for u in page_urls:
                    if u not in urls:
                        urls.append(u)
        finally:
            await pg.close()
            await b.close()
    return urls


def list_match_urls(num_pages: int = 2) -> list[str]:
    return asyncio.run(_collect(num_pages))


def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    urls = list_match_urls(n)
    print(f"# {len(urls)} listing URLs across {n} page(s)")
    for u in urls:
        print(u)
    return 0


if __name__ == "__main__":
    sys.exit(main())
