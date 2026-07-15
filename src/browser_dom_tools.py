"""Shared raw-DOM browser helpers, connecting over CDP directly with Playwright.

A narrow fallback for situations the browser backend's accessibility-tree
snapshot doesn't handle well: an open dialog/overlay whose markup lacks
proper ARIA roles (so it never gets a ref in browser_snapshot), a
snapshot-ref click that silently no-ops, and a text input inside such a
dialog that has no ref to pass to browser_type/browser_fill_form. These
query real DOM selectors instead of the accessibility tree, click/fill by
visible text or label instead of an accessibility-tree ref -- so they keep
working exactly where the ref-based flow doesn't.

Deliberately NOT arbitrary JS execution (no browser_evaluate equivalent):
each function runs one fixed, narrowly-scoped operation.
"""
from __future__ import annotations

import json
import re

from .dashboard.data import redact


async def current_page(browser, hint_url: str | None = None):
    """Pick the page the browser MCP itself considers "current".

    With several tabs open (common on REBO-style flows: SSO popups, an
    inschrijfportaal tab, the original listing tab...) the last-*created*
    page is NOT reliably the one browser_snapshot/browser_click are acting
    on -- verified in production (Hof van Oslo, 01-07-2026 retest): dom_scan
    kept reporting a stale tab while the MCP had already moved on, sending
    the agent chasing a dialog on the wrong page for a dozen turns. Neither
    document.visibilityState nor document.hasFocus() distinguish tabs in
    this headed-but-no-real-window-manager (WSLg/Xvfb) setup -- verified
    directly, all tabs report visible/focused. The MCP's own `browser_tabs`
    listing marks the true current tab with "(current)"; the caller passes
    that tab's URL through as hint_url so we match on real ground truth
    instead of a heuristic.
    """
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    if not ctx.pages:
        return await ctx.new_page()
    if hint_url:
        for page in ctx.pages:
            if page.url == hint_url:
                return page
    return ctx.pages[-1]


async def dialog_scope(page):
    """A Locator scoped to the currently open <dialog>, or None if none is open.

    Verified on REBO Groep (Hof van Oslo, 02-07-2026): a single page can have
    several <dialog> elements (viewing request, brochure download, email-
    service upsell) that all reuse the SAME field ids (id="first_name" etc,
    invalid but real HTML). An unscoped getElementById/get_by_label/get_by_text
    silently resolves to whichever hidden dialog comes first in DOM order --
    not the one actually open -- so a fill silently no-ops (0x0 bounding box)
    and a click can time out waiting on a hidden duplicate. Every read/click/
    fill below scopes to the open dialog first so it always targets the
    thing actually on screen.
    """
    dlg = page.locator("dialog[open]")
    try:
        if await dlg.count() > 0:
            return dlg.first
    except Exception:
        pass
    return None


async def evaluate_controls(scope) -> list[dict]:
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
        return await scope.locator(
            "a, button, input[type=button], input[type=submit], [role=button]"
        ).evaluate_all(script)
    except Exception:
        return []


async def evaluate_fields(scope) -> list[dict]:
    script = """
    els => els.map(el => {
      const id = el.id || '';
      const label = id ? el.closest('body,dialog').querySelector(`label[for="${CSS.escape(id)}"]`) : null;
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
        return await scope.locator("input, textarea, select").evaluate_all(script)
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
    e.g. an open dialog/overlay built from unlabeled <div>s.

    Scoped to the open <dialog> when one exists (see dialog_scope) so
    duplicate-id fields in other, hidden dialogs on the same page don't
    show up and confuse the model (verified: a dialog-unaware dom_scan
    showed "two identical sets of fields" for the same reason)."""
    scope = await dialog_scope(page)
    root = scope if scope is not None else page
    body_text = ""
    try:
        text_source = page.locator("body") if scope is None else scope
        body_text = await text_source.inner_text(timeout=5000)
    except Exception:
        pass
    report = {
        "url": page.url,
        "title": await page.title(),
        "in_open_dialog": scope is not None,
        "text_excerpt": compact(body_text, 4000),
        "buttons_and_links": (await evaluate_controls(root))[:80],
        "form_fields": (await evaluate_fields(root))[:80],
    }
    return redact(json.dumps(report, ensure_ascii=False, indent=2))


async def dom_scan(cdp_url: str, settle_ms: int = 500, current_url: str | None = None) -> str:
    """Connect over CDP, wait briefly for any just-opened dialog/overlay to
    finish rendering, then return a raw-DOM report of the current page.

    current_url, when given, is the MCP's own idea of the current tab (see
    current_page) -- pass it whenever the caller knows it so this reports on
    the same tab the model has been looking at.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            page = await current_page(browser, hint_url=current_url)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await dom_report(page)
        finally:
            await browser.close()


# Cookie/consent buttons, most-preferred first. Pass 1 accepts everything in
# one click (multi-word phrases + well-known CMP button ids only -- a bare
# "Akkoord"/"Accepteren" could hit a real form button). Pass 2 closes a
# preferences-style dialog ("Opslaan en sluiten" -- the exact overlay that
# ate a run's final turns on huurportaal/rebogroep, 05-07-2026).
_COOKIE_ACCEPT_SELECTORS = [
    "#onetrust-accept-btn-handler",
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "#didomi-notice-agree-button",
    ".cmplz-accept",
] + [
    f'{base}:has-text("{t}")'
    for t in ("Alles accepteren", "Accepteer alles", "Accepteer alle cookies",
              "Alle cookies toestaan", "Alles toestaan", "Cookies accepteren",
              "Accept all", "Allow all cookies")
    for base in ("button", "[role=button]", "a")
]
_COOKIE_CLOSE_SELECTORS = [
    f'{base}:has-text("{t}")'
    for t in ("Opslaan en sluiten", "Sluiten en doorgaan", "Save and close")
    for base in ("button", "[role=button]", "a")
]

_CONSENT_SYNC_URL_MARKERS = (
    "user-sync", "usersync", "sync.privacy", "sync.ad", "adnxs.com",
    "doubleclick.net", "rubiconproject.com", "pubmatic.com", "criteo.com",
    "bing.com/fd/ls",
)


def _is_consent_sync_url(url: str) -> bool:
    low = (url or "").lower()
    return any(marker in low for marker in _CONSENT_SYNC_URL_MARKERS)


async def _close_consent_sync_tabs(browser, preferred_url: str | None) -> int:
    """Close adtech/user-sync tabs opened by consent managers.

    Pararius consent has been seen to open/select a user-sync tab after the
    auto-dismiss click, so the next agent turn wastes time on an irrelevant
    page. Only close known sync/adtech URLs, and bring the original page back
    to front when it is still present.
    """
    closed = 0
    for ctx in browser.contexts:
        pages = list(ctx.pages)
        keep = None
        if preferred_url:
            for page in pages:
                if page.url == preferred_url and not _is_consent_sync_url(page.url):
                    keep = page
                    break
        if keep is None:
            keep = next((p for p in pages if not _is_consent_sync_url(p.url)), None)
        for page in pages:
            if page is keep:
                continue
            if _is_consent_sync_url(page.url):
                try:
                    await page.close()
                    closed += 1
                except Exception:
                    pass
        if keep is not None:
            try:
                await keep.bring_to_front()
            except Exception:
                pass
    return closed


async def dismiss_cookie_banner(cdp_url: str, current_url: str | None = None) -> str | None:
    """Deterministically click away a cookie/consent banner on the current
    page, if one is present. Returns a short note when something was
    dismissed, None when nothing matched (the common case, and cheap).

    Called automatically after every navigation (browser_agent) so consent
    overlays -- which intercept ALL clicks until dealt with -- never cost
    LLM turns. Fixed, narrow operation like the other helpers here: it only
    ever clicks a known consent-accept/close control.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            page = await current_page(browser, hint_url=current_url)
            for kind, selectors in (("accepted", _COOKIE_ACCEPT_SELECTORS),
                                    ("closed", _COOKIE_CLOSE_SELECTORS)):
                loc = page.locator(", ".join(selectors)).first
                try:
                    await loc.click(timeout=1200)
                    await page.wait_for_timeout(400)
                    closed = await _close_consent_sync_tabs(browser, page.url)
                    note = f"{kind} a cookie/consent banner"
                    if closed:
                        note += f" and closed {closed} consent sync tab(s)"
                    return note
                except Exception:  # noqa: BLE001 - absence is the normal case
                    continue
            await _close_consent_sync_tabs(browser, current_url)
            return None
        finally:
            await browser.close()


async def click_by_text(cdp_url: str, text: str, settle_ms: int = 600, current_url: str | None = None) -> str:
    """Connect over CDP and click the first element whose visible text
    matches, bypassing the accessibility-tree ref system entirely -- for
    elements a snapshot-based browser_click can't target. Scoped to the open
    dialog first (see dialog_scope) so it can't hit a hidden duplicate.

    current_url: see dom_scan -- the MCP's own current-tab URL, so the click
    lands on the tab the model actually meant.
    """
    label = " ".join(str(text or "").split())
    if not label:
        return "REFUSED: empty click text"

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            page = await current_page(browser, hint_url=current_url)
            scope = await dialog_scope(page)
            root = scope if scope is not None else page
            try:
                await root.get_by_text(label, exact=False).first.click(timeout=7000)
            except Exception as e:
                # A miss here (wrong/stale text, element not clickable, strict-mode
                # ambiguity...) must come back as a normal tool result the model can
                # react to -- letting it escape as an exception previously killed
                # the whole MCP session/agent run outright (verified in production,
                # Hof van Oslo retest 01-07-2026: a 7s Locator.click timeout for
                # "Sluiten" propagated out of the async_playwright block and crashed
                # the process mid-run -- root cause turned out to be exactly the
                # duplicate-dialog-id problem dialog_scope now fixes).
                return f"CLICK FAILED: no clickable element matched {label!r} ({type(e).__name__}). {await dom_report(page)}"
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await dom_report(page)
        finally:
            await browser.close()


# huurwoningen.nl's aggregator gateway: a "Contact met de verhuurder" control
# (its visible text varies by listing -- verified: "Bekijk opnieuw" and
# "Reageer op deze woning" both seen) opens a dialog reading "Deze woning is
# gevonden buiten ons eigen netwerk. Met de knop hieronder kom je direct bij
# de aanbieder terecht." with an opt-in checkbox (left unchecked -- it is
# promotional, not required) and a "Ga verder" button that lands on the real
# external provider. A paired replay (10-07-2026) showed this costing 7-19
# turns of browser_find/dom_scan trial-and-error per session, once ballooning
# a single session from 85k to 211k tokens. This fixes the whole two-click
# gateway as one deterministic tool call instead of leaving the model to
# rediscover it turn by turn.
_AGGREGATOR_STEP1_TEXTS = ("Bekijk opnieuw", "Reageer op deze woning", "Reageer")
_AGGREGATOR_STEP2_TEXTS = ("Ga verder",)


async def aggregator_hop(cdp_url: str, settle_ms: int = 700, current_url: str | None = None) -> str:
    """Connect over CDP and drive the huurwoningen.nl aggregator gateway in
    one call: click the first matching "Contact met de verhuurder" control,
    then click "Ga verder" in the dialog it opens. Falls back to reporting
    the raw DOM (like click_by_text) when either step doesn't match, so the
    model can recover with the narrower fallback tools instead of being stuck.
    """
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            page = await current_page(browser, hint_url=current_url)
            start_url = page.url
            clicked_first = None
            for label in _AGGREGATOR_STEP1_TEXTS:
                try:
                    await page.get_by_text(label, exact=False).first.click(timeout=2500)
                    clicked_first = label
                    break
                except Exception:
                    continue
            if clicked_first is None:
                return (
                    "AGGREGATOR HOP FAILED: none of the gateway controls "
                    f"({', '.join(_AGGREGATOR_STEP1_TEXTS)!r}) were clickable on this "
                    f"page. {await dom_report(page)}"
                )
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            scope = await dialog_scope(page)
            root = scope if scope is not None else page
            clicked_second = None
            for label in _AGGREGATOR_STEP2_TEXTS:
                try:
                    await root.get_by_text(label, exact=False).first.click(timeout=5000)
                    clicked_second = label
                    break
                except Exception:
                    continue
            if clicked_second is None:
                return (
                    f"AGGREGATOR HOP PARTIAL: clicked {clicked_first!r} but no "
                    f"{'/'.join(_AGGREGATOR_STEP2_TEXTS)} control appeared to confirm. "
                    f"{await dom_report(page)}"
                )
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return (
                f"AGGREGATOR HOP OK: clicked {clicked_first!r} then {clicked_second!r}. "
                f"URL {start_url!r} -> {page.url!r}.\n{await dom_report(page)}"
            )
        finally:
            await browser.close()


async def fill_by_label(cdp_url: str, label: str, value: str, settle_ms: int = 300,
                         current_url: str | None = None) -> str:
    """Connect over CDP and fill the text/email/tel/textarea input associated
    with the given <label> text, bypassing the accessibility-tree ref system.

    This is the missing piece dom_scan/click_by_text alone don't cover: a
    dialog without ARIA roles can be READ (dom_scan) and its buttons CLICKED
    (click_by_text), but until this tool existed there was no way to TYPE
    into one of its text inputs at all -- browser_type/browser_fill_form need
    a browser_snapshot ref, and click_by_text only clicks. Verified missing
    in production (Hof van Oslo, 02-07-2026): the agent reached REBO's real
    "Bezichtiging aanvragen" dialog every time but could never fill Voornaam/
    Achternaam/etc because no tool could reach the input.

    Scoped to the open dialog first (see dialog_scope), AND deliberately does
    NOT use Playwright's get_by_label -- that resolves a <label for=id> via a
    document-wide id lookup internally, so it breaks the same way plain
    getElementById does when ids repeat across dialogs (verified directly:
    REBO Groep reuses id="first_name" etc across 3 dialogs on one page --
    get_by_label scoped to the open dialog still timed out, resolving to a
    hidden duplicate outside the scope root and never finding a match inside
    it). Instead this finds the <label> BY TEXT within the scope (which does
    respect scoping, verified), then the input/textarea inside its nearest
    container -- the same proximity approach select_option_by_label uses for
    its button.
    """
    field_label = " ".join(str(label or "").split())
    if not field_label:
        return "REFUSED: empty label"

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            page = await current_page(browser, hint_url=current_url)
            scope = await dialog_scope(page)
            root = scope if scope is not None else page
            label_loc = root.get_by_text(field_label, exact=False).first
            try:
                container = label_loc.locator(
                    "xpath=ancestor::*[.//input or .//textarea][1]"
                ).first
                await container.locator("input, textarea").first.fill(str(value), timeout=7000)
            except Exception as e:
                return (
                    f"FILL FAILED: no input labelled {field_label!r} ({type(e).__name__}). "
                    f"{await dom_report(page)}"
                )
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await dom_report(page)
        finally:
            await browser.close()


async def select_option_by_label(cdp_url: str, label: str, option: str, settle_ms: int = 400,
                                  current_url: str | None = None) -> str:
    """Connect over CDP and operate a custom (non-<select>) dropdown: click
    the control associated with the given label to open it, then click the
    option matching the given visible text.

    Some sites (verified: REBO Groep's "Soort inkomen" field) build dropdowns
    as a <label> + sibling <button> with no text of its own (an icon only),
    so click_by_text can't target the button by label text -- clicking the
    label itself doesn't work either, the sibling button intercepts pointer
    events (verified directly). This finds the nearest ancestor of the label
    that also contains a button, clicks that button to open the option list,
    then clicks the option text -- both scoped to the open dialog (see
    dialog_scope).
    """
    field_label = " ".join(str(label or "").split())
    option_text = " ".join(str(option or "").split())
    if not field_label or not option_text:
        return "REFUSED: empty label or option"

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            page = await current_page(browser, hint_url=current_url)
            scope = await dialog_scope(page)
            root = scope if scope is not None else page
            label_loc = root.get_by_text(field_label, exact=False).first
            try:
                container = label_loc.locator(
                    "xpath=ancestor::*[.//button][1]"
                ).first
                await container.locator("button").first.click(timeout=7000)
            except Exception as e:
                return (
                    f"SELECT FAILED: could not open dropdown for {field_label!r} "
                    f"({type(e).__name__}). {await dom_report(page)}"
                )
            await page.wait_for_timeout(settle_ms)
            try:
                # Option buttons on real sites often have no explicit
                # type="button" (verified: REBO Groep's income-type options),
                # so their default type is "submit" -- clicking one could
                # fire a real, premature form submission before the agent
                # finishes filling the rest of the dialog. A one-time capturing
                # guard on every form absorbs that native submit harmlessly
                # (the site's own click handler, which updates the visible
                # selection, still runs -- click handlers fire before submit)
                # without touching the later, deliberate Verzenden click.
                await page.evaluate("""
                () => {
                  document.querySelectorAll('form').forEach(f => {
                    f.addEventListener('submit', e => {
                      e.preventDefault(); e.stopImmediatePropagation();
                    }, {capture: true, once: true});
                  });
                }
                """)
                await root.get_by_text(option_text, exact=False).first.click(timeout=7000)
            except Exception as e:
                return (
                    f"SELECT FAILED: dropdown for {field_label!r} opened but option "
                    f"{option_text!r} was not found ({type(e).__name__}). {await dom_report(page)}"
                )
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await dom_report(page)
        finally:
            await browser.close()


async def check_by_label(cdp_url: str, label: str, settle_ms: int = 300,
                         current_url: str | None = None) -> str:
    """Connect over CDP and programmatically set input.checked=true for the
    checkbox associated with the given label text — without clicking.

    REBO Groep's "Bezichtiging aanvragen" dialog (reached via huurwoningen.nl's
    aggregator_hop) has three custom-styled checkboxes invisible to the
    accessibility tree. click_by_text clicks the label but does not toggle the
    checkbox state; the MCP browser_check tool only hits the first
    querySelector('input[type="checkbox"]') match; and browser_click on the
    hidden <input> can trigger site JavaScript that closes the dialog entirely.

    This sets the checked state directly via page.evaluate() and dispatches
    synthetic change/input events — bypassing click-event handlers entirely. It
    finds the correct checkbox through label proximity (for/id first, then
    ancestor/sibling search), so it works even when custom styling hides the
    input from the accessibility tree.

    Scoped to the open dialog first (see dialog_scope) so duplicate-id fields
    in other, hidden dialogs on the same page are never targeted.

    Labels without an associated input return a descriptive failure so the
    model can try a different label.
    """
    field_label = " ".join(str(label or "").split())
    if not field_label:
        return "REFUSED: empty label"

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_url, timeout=30000)
        try:
            page = await current_page(browser, hint_url=current_url)
            scope = await dialog_scope(page)
            root = scope if scope is not None else page
            label_loc = root.get_by_text(field_label, exact=False).first
            try:
                await label_loc.evaluate("""
                (labelEl) => {
                  let input = null;
                  // Standard for/id association first
                  if (labelEl.htmlFor) {
                    input = labelEl.closest('body,dialog')?.querySelector(
                      '#' + CSS.escape(labelEl.htmlFor)
                    );
                  }
                  // Fall back: nearest ancestor with a checkbox
                  if (!input) {
                    let ancestor = labelEl;
                    for (let i = 0; i < 8 && ancestor && !input; i++) {
                      ancestor = ancestor.parentElement;
                      if (ancestor) {
                        input = ancestor.querySelector('input[type="checkbox"]');
                      }
                    }
                  }
                  // Broader fallback: sibling walk within container
                  if (!input) {
                    const container = labelEl.closest('label,div,fieldset') || labelEl.parentElement;
                    if (container) {
                      input = container.querySelector('input[type="checkbox"]');
                    }
                  }
                  if (!input) throw new Error('NO_CHECKBOX_FOUND');
                  input.checked = true;
                  input.dispatchEvent(new Event('change', {bubbles: true}));
                  input.dispatchEvent(new Event('input', {bubbles: true}));
                  return input.getAttribute('name') || input.id || '(anonymous)';
                }
                """)
            except Exception as e:
                return (
                    f"CHECK FAILED: no checkbox associated with label {field_label!r} "
                    f"({type(e).__name__}). {await dom_report(page)}"
                )
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await dom_report(page)
        finally:
            await browser.close()

