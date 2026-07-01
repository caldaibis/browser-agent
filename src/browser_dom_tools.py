"""Shared raw-DOM browser helpers, connecting over CDP directly with Playwright.

A narrow fallback for two situations the Playwright MCP's accessibility-tree
snapshot doesn't handle well: an open dialog/overlay whose markup lacks
proper ARIA roles (so it never gets a ref in browser_snapshot), and a
snapshot-ref click that silently no-ops. These query real DOM selectors
instead of the accessibility tree, and click by visible text instead of an
accessibility-tree ref -- so both keep working exactly where the ref-based
flow doesn't.

Deliberately NOT arbitrary JS execution (no browser_evaluate equivalent):
each function runs one fixed, narrowly-scoped operation.
"""
from __future__ import annotations

import json
import re

from .dashboard.data import redact


async def current_page(browser):
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    if ctx.pages:
        return ctx.pages[-1]
    return await ctx.new_page()


async def evaluate_controls(page) -> list[dict]:
    script = """
    els => els.map(el => {
      const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
        el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
      const href = el.href || el.getAttribute('href') || '';
      return {
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        text: text.slice(0, 160),
        href: href.slice(0, 240),
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
      };
    }).filter(x => x.text || x.href)
    """
    try:
        return await page.locator(
            "a, button, input[type=button], input[type=submit], [role=button]"
        ).evaluate_all(script)
    except Exception:
        return []


async def evaluate_fields(page) -> list[dict]:
    script = """
    els => els.map(el => {
      const id = el.id || '';
      const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
      const text = (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
        (label && label.innerText) || el.getAttribute('name') || id || '')
        .replace(/\\s+/g, ' ').trim();
      return {
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        label: text.slice(0, 160),
        required: !!el.required || el.getAttribute('aria-required') === 'true',
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
      };
    }).filter(x => x.label || x.type)
    """
    try:
        return await page.locator("input, textarea, select").evaluate_all(script)
    except Exception:
        return []


def compact(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " ...[truncated]"


async def dom_report(page) -> str:
    """Raw-DOM snapshot of the current page: title/url/text + every button,
    link, and form field found by direct DOM query (not the accessibility
    tree) -- so it surfaces controls an accessibility-tree snapshot misses,
    e.g. an open dialog/overlay built from unlabeled <div>s."""
    body_text = ""
    try:
        body_text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass
    report = {
        "url": page.url,
        "title": await page.title(),
        "text_excerpt": compact(body_text, 4000),
        "buttons_and_links": (await evaluate_controls(page))[:80],
        "form_fields": (await evaluate_fields(page))[:80],
    }
    return redact(json.dumps(report, ensure_ascii=False, indent=2))


async def dom_scan(cdp_url: str, settle_ms: int = 500) -> str:
    """Connect over CDP, wait briefly for any just-opened dialog/overlay to
    finish rendering, then return a raw-DOM report of the current page."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        try:
            page = await current_page(browser)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await dom_report(page)
        finally:
            await browser.close()


async def click_by_text(cdp_url: str, text: str, settle_ms: int = 600) -> str:
    """Connect over CDP and click the first element whose visible text
    matches, bypassing the accessibility-tree ref system entirely -- for
    elements a snapshot-based browser_click can't target."""
    label = " ".join(str(text or "").split())
    if not label:
        return "REFUSED: empty click text"

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url)
        try:
            page = await current_page(browser)
            await page.get_by_text(label, exact=False).first.click(timeout=7000)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await dom_report(page)
        finally:
            await browser.close()
