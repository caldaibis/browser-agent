"""Deterministic site-specific apply fast paths.

These run before the LLM agent while the shared browser lock is already held.
They are intentionally narrow: if a page does not match a known simple flow, the
function returns None and the regular browser agent takes over.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from .browser_agent import AgentResult

_PARARIUS_HOSTS = {"pararius.nl"}
_CONFIRM_RE = re.compile(
    r"(je reactie is verstuurd|reactie verstuurd|message sent|your response has been sent)",
    re.IGNORECASE,
)
_ALREADY_RE = re.compile(
    r"(je hebt al gereageerd|already responded|reactie is al verstuurd)",
    re.IGNORECASE,
)
_LOGIN_RE = re.compile(r"(inloggen|log in|login|wachtwoord|password)", re.IGNORECASE)


def _domain(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _log(path: Path, line: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"[fastpath] {line}\n")
    except Exception:
        pass


def _first_visible(locator):
    for i in range(locator.count()):
        item = locator.nth(i)
        try:
            if item.is_visible(timeout=200):
                return item
        except Exception:
            continue
    return None


def _click_first_text(page, labels: tuple[str, ...], timeout: int = 2000) -> bool:
    for label in labels:
        loc = page.get_by_text(label, exact=False)
        item = _first_visible(loc)
        if item is None:
            continue
        try:
            item.click(timeout=timeout)
            page.wait_for_timeout(500)
            return True
        except Exception:
            continue
    return False


def _fill_message(page, message: str) -> bool:
    candidates = [
        page.locator("textarea"),
        page.locator(
            "input[name*='message' i], input[name*='bericht' i], "
            "input[name*='motiv' i]"
        ),
        page.get_by_label(re.compile(r"(bericht|message|motivatie|toelichting|opmerking)", re.I)),
    ]
    for loc in candidates:
        item = _first_visible(loc)
        if item is None:
            continue
        try:
            item.fill(message[:1800], timeout=2000)
            return True
        except Exception:
            continue
    return False


def _submit(page) -> bool:
    return _click_first_text(
        page,
        (
            "Reactie versturen", "Verstuur reactie", "Versturen", "Verzenden",
            "Send response", "Send message", "Submit",
        ),
        timeout=2500,
    )


def _try_pararius(cdp_url: str, url: str, message: str, log_path: Path) -> AgentResult | None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1500)
            text = page.locator("body").inner_text(timeout=5000)
            if _ALREADY_RE.search(text):
                return AgentResult(
                    rc=0,
                    outcome="already_applied",
                    summary="Pararius fast path found an existing response and did not resubmit.",
                    resolved_url=page.url,
                )
            if _CONFIRM_RE.search(text):
                return AgentResult(
                    rc=0,
                    outcome="already_applied",
                    summary="Pararius fast path found a sent-response confirmation and did not resubmit.",
                    resolved_url=page.url,
                )
            clicked = _click_first_text(
                page,
                (
                    "Reageer op deze woning", "Reageer", "Contact met de aanbieder",
                    "Bericht sturen", "Plan een bezichtiging", "Vraag bezichtiging aan",
                ),
            )
            if not clicked:
                _log(log_path, "pararius unsupported: no visible response/contact control")
                return None
            page.wait_for_timeout(1200)
            text = page.locator("body").inner_text(timeout=5000)
            if _LOGIN_RE.search(text) and not page.locator("textarea").count():
                _log(log_path, "pararius unsupported: login flow visible")
                return None
            _fill_message(page, message)
            if not _submit(page):
                _log(log_path, "pararius unsupported: no submit control")
                return None
            page.wait_for_timeout(2500)
            text = page.locator("body").inner_text(timeout=5000)
            if _CONFIRM_RE.search(text):
                return AgentResult(
                    rc=0,
                    outcome="submitted",
                    summary="Pararius fast path submitted the response successfully.",
                    resolved_url=page.url,
                )
            _log(log_path, "pararius submit clicked but no confirmation detected")
            return None
        except Exception as e:  # noqa: BLE001 - fast paths are optional
            _log(log_path, f"pararius failed, falling back: {type(e).__name__}: {e}")
            return None
        finally:
            try:
                browser.close()
            except Exception:
                pass


def try_fast_apply(listing: dict, cdp_url: str, log_path: Path,
                   message: str) -> AgentResult | None:
    """Return AgentResult when a deterministic site flow completes, else None."""
    url = listing.get("source_url", "")
    domain = _domain(url)
    if domain in _PARARIUS_HOSTS:
        _log(log_path, f"trying pararius fast path for {url}")
        return _try_pararius(cdp_url, url, message, log_path)
    return None
