"""Network sniffer for tier-1 API discovery — the DevTools "Network" tab, in code.

Loads a site's listing page in a real (JS-rendering) Chromium and records every
JSON/XHR response, so we can find the JSON endpoint that feeds an SPA's listing
list without hand-copying a cURL. Response bodies are saved under
``logs/sniff/<host>/`` for inspection; a ranked summary (biggest JSON first,
which is almost always the listing feed) is printed.

Run:
  python -m src.poller.sniff https://ikwilhuren.nu/aanbod
  python -m src.poller.sniff <site-name-from-registry>   # uses its list_url
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

from ..config import LOG_DIR
from .registry import by_name

SNIFF_DIR = LOG_DIR / "sniff"

# URL fragments that usually indicate a data API (vs. analytics/assets).
_API_HINT = re.compile(r"(/api/|/graphql|/wp-json|search|aanbod|woning|listing|"
                       r"propert|zoek|rent|huur|result)", re.I)
# Noise we never care about.
_NOISE = re.compile(r"(google|gtag|analytics|facebook|hotjar|cookiebot|sentry|"
                    r"doubleclick|cdn\.|\.png|\.jpg|\.svg|\.woff|\.css|\.js($|\?))", re.I)


def sniff(url: str, wait_ms: int = 6000) -> None:
    host = urlparse(url).hostname or "site"
    out_dir = SNIFF_DIR / host
    out_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/126.0.0.0 Safari/537.36"),
            locale="nl-NL",
        )
        page = ctx.new_page()

        def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                u = resp.url
                if _NOISE.search(u):
                    return
                is_json = "json" in ct or resp.request.resource_type in ("xhr", "fetch")
                if not is_json:
                    return
                body = resp.body()
                records.append({
                    "url": u, "status": resp.status, "ct": ct,
                    "method": resp.request.method, "size": len(body),
                    "hint": bool(_API_HINT.search(u)), "body": body,
                })
            except Exception:
                pass

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception as e:
            print(f"(navigation note: {e})")
        page.wait_for_timeout(wait_ms)
        browser.close()

    # Rank: API-hinted JSON first, then by size (listing feeds are large).
    records.sort(key=lambda r: (r["hint"], r["size"]), reverse=True)
    ts = datetime.now().strftime("%H%M%S")
    print(f"\n# {len(records)} JSON/XHR responses from {url}\n")
    for i, r in enumerate(records[:25]):
        tag = "API?" if r["hint"] else "    "
        fname = out_dir / f"{ts}_{i:02d}.json"
        try:
            fname.write_bytes(r["body"])
        except Exception:
            fname = None
        preview = ""
        try:
            data = json.loads(r["body"])
            if isinstance(data, dict):
                preview = "keys: " + ", ".join(list(data.keys())[:8])
            elif isinstance(data, list):
                preview = f"array[{len(data)}]"
        except Exception:
            preview = "(non-JSON body)"
        print(f"[{tag}] {r['status']} {r['method']:4} {r['size']:>8}B  {r['url']}")
        print(f"        {preview}   saved={fname.name if fname else '-'}")
    print(f"\nBodies saved under {out_dir}")


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python -m src.poller.sniff <url-or-registry-name>")
        return 2
    arg = sys.argv[1]
    site = by_name(arg)
    url = site.list_url if site else arg
    sniff(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
