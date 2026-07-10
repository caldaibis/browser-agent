"""Read-mostly browser diagnostics tools over the shared CDP browser.

Custom MCP tools (browser_open / browser_diagnostics / browser_safe_click /
browser_screenshot), guarded by the cross-process browser lock and a
blocked-click policy: diagnostic navigation only, never submit/destroy."""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from claude_agent_sdk import McpSdkServerConfig, create_sdk_mcp_server, tool

from ..browser_dom_tools import compact, current_page, evaluate_controls, evaluate_fields
from ..config import CDP_URL, SCREENSHOT_DIR
from ..poller.browser_lock import browser_lock
from ..redaction import redact
from ..settings import settings


_BLOCKED_CLICK_RE = re.compile(
    r"("
    r"submit|send|apply|verzend|verstuur|reageer|solliciteer|"
    r"aanvraag|aanvragen|bezichtiging|inschrijven|"
    r"wijzig|modify|change|intrekken|withdraw|cancel|delete|remove|"
    r"wachtwoord|password|forgot|reset|account verwijderen"
    r")",
    re.IGNORECASE,
)
_LOCK_TIMEOUT = settings().self_improvement_browser_lock_timeout


async def _browser_open(url: str, settle_ms: int) -> str:
    if not _safe_browser_url(url):
        return f"REFUSED: unsafe browser URL: {url!r}"
    return await asyncio.to_thread(_browser_open_locked, url, _clamp_settle(settle_ms))


async def _browser_diagnostics(settle_ms: int) -> str:
    return await asyncio.to_thread(_browser_diagnostics_locked, _clamp_settle(settle_ms))


async def _browser_safe_click(text: str, settle_ms: int) -> str:
    label = " ".join(str(text or "").split())
    if not label:
        return "REFUSED: empty click text"
    if _blocked_click_label(label):
        return f"REFUSED: click label is potentially submitting/destructive: {label!r}"
    return await asyncio.to_thread(_browser_safe_click_locked, label, _clamp_settle(settle_ms))


async def _browser_screenshot(full_page: bool) -> str:
    return await asyncio.to_thread(_browser_screenshot_locked, full_page)


def _safe_browser_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", str(url or ""), re.IGNORECASE))


def _blocked_click_label(text: str) -> bool:
    return bool(_BLOCKED_CLICK_RE.search(text or ""))


def _clamp_settle(ms: int) -> int:
    return max(0, min(int(ms or 0), 10000))


def _browser_open_locked(url: str, settle_ms: int) -> str:
    with browser_lock(timeout=_LOCK_TIMEOUT, holder="self-improvement"):
        return asyncio.run(_browser_open_async(url, settle_ms))


def _browser_diagnostics_locked(settle_ms: int) -> str:
    with browser_lock(timeout=_LOCK_TIMEOUT, holder="self-improvement"):
        return asyncio.run(_browser_diagnostics_async(settle_ms))


def _browser_safe_click_locked(text: str, settle_ms: int) -> str:
    with browser_lock(timeout=_LOCK_TIMEOUT, holder="self-improvement"):
        return asyncio.run(_browser_safe_click_async(text, settle_ms))


def _browser_screenshot_locked(full_page: bool) -> str:
    with browser_lock(timeout=_LOCK_TIMEOUT, holder="self-improvement"):
        return asyncio.run(_browser_screenshot_async(full_page))


async def _browser_open_async(url: str, settle_ms: int) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL, timeout=10000)
        try:
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            events = _attach_browser_event_collectors(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await _page_report(page, events, include_screenshot=False)
        finally:
            await browser.close()


async def _browser_diagnostics_async(settle_ms: int) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL, timeout=10000)
        try:
            page = await current_page(browser)
            events = _attach_browser_event_collectors(page)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await _page_report(page, events, include_screenshot=False)
        finally:
            await browser.close()


async def _browser_safe_click_async(text: str, settle_ms: int) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL, timeout=10000)
        try:
            page = await current_page(browser)
            events = _attach_browser_event_collectors(page)
            await page.get_by_text(text, exact=False).first.click(timeout=7000)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await _page_report(page, events, include_screenshot=False)
        finally:
            await browser.close()


async def _browser_screenshot_async(full_page: bool) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL, timeout=10000)
        try:
            page = await current_page(browser)
            path = SCREENSHOT_DIR / f"self_improvement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=full_page, timeout=30000)
            report = json.loads(await _page_report(
                page,
                {"console": [], "network": []},
                include_screenshot=False,
            ))
            report["screenshot_path"] = str(path)
            return redact(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            await browser.close()


def _attach_browser_event_collectors(page) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {"console": [], "network": []}

    def on_console(msg) -> None:
        if msg.type in {"error", "warning"}:
            events["console"].append(f"{msg.type}: {msg.text}"[:500])

    def on_response(resp) -> None:
        if resp.status >= 400:
            events["network"].append(f"{resp.status} {resp.url}"[:500])

    page.on("console", on_console)
    page.on("response", on_response)
    return events


async def _page_report(page, events: dict[str, list[str]], *, include_screenshot: bool) -> str:
    body_text = ""
    try:
        body_text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass

    controls = await evaluate_controls(page)
    fields = await evaluate_fields(page)
    report = {
        "url": page.url,
        "title": await page.title(),
        "text_excerpt": compact(body_text, 6000),
        "buttons_and_links": controls[:80],
        "form_fields": fields[:80],
        "console_errors": events.get("console", [])[-20:],
        "network_errors": events.get("network", [])[-30:],
    }
    if include_screenshot:
        path = SCREENSHOT_DIR / f"self_improvement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True, timeout=30000)
        report["screenshot_path"] = str(path)
    return redact(json.dumps(report, ensure_ascii=False, indent=2))


def _browser_tools() -> McpSdkServerConfig:
    @tool("browser_open", (
        "Open a URL in the shared CDP browser under the browser lock and "
        "return safe diagnostics. Use this to verify listing/page state."
    ), {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "settle_ms": {"type": "integer", "default": 2500},
        },
        "required": ["url"],
    })
    async def browser_open(args: dict) -> dict:
        text = await _browser_open(str(args.get("url") or ""), int(args.get("settle_ms") or 2500))
        return {"content": [{"type": "text", "text": text}]}

    @tool("browser_diagnostics", (
        "Inspect the current shared-browser page under the browser lock and "
        "return URL/title/text excerpt/buttons/links/forms/errors."
    ), {
        "type": "object",
        "properties": {"settle_ms": {"type": "integer", "default": 1000}},
        "required": [],
    })
    async def browser_diagnostics(args: dict) -> dict:
        text = await _browser_diagnostics(int(args.get("settle_ms") or 1000))
        return {"content": [{"type": "text", "text": text}]}

    @tool("browser_safe_click", (
        "Click visible text only for benign navigation or cookie banners. "
        "Refuses submit/apply/withdraw/password/account-destructive labels."
    ), {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "settle_ms": {"type": "integer", "default": 1500},
        },
        "required": ["text"],
    })
    async def browser_safe_click(args: dict) -> dict:
        text = await _browser_safe_click(str(args.get("text") or ""), int(args.get("settle_ms") or 1500))
        return {"content": [{"type": "text", "text": text}]}

    @tool("browser_screenshot", (
        "Save a screenshot of the current shared-browser page and return "
        "the file path plus diagnostics."
    ), {
        "type": "object",
        "properties": {"full_page": {"type": "boolean", "default": True}},
        "required": [],
    })
    async def browser_screenshot(args: dict) -> dict:
        text = await _browser_screenshot(bool(args.get("full_page", True)))
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(
        name="browser",
        tools=[browser_open, browser_diagnostics, browser_safe_click, browser_screenshot],
    )
