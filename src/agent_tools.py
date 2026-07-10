"""The apply agent's local tool surface: schemas + blocked-tools policy.

These are the OpenAI-format tool definitions for the four raw-DOM
fallback tools (implemented in `browser_dom_tools.py`) and the
credential lookup, plus the Playwright MCP tools the model must never
see. Pure data — the loop in `browser_agent.py` wires them up.
"""
from __future__ import annotations

# Local (non-MCP) tool: look up a stored site login by domain/URL on demand, so
# credentials never sit in the prompt and the agent can fetch whichever site it
# actually lands on (a single application can span multiple hosts).
CREDENTIAL_TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_credential",
        "description": (
            "Return the stored username/password for a rental site. Pass the "
            "site's domain or current URL (e.g. 'ikwilhuren.nu'). Use this for "
            "every email/password login instead of guessing; returns an error "
            "string if no credential is stored for that site. Some listing "
            "sites redirect their login to a SHARED third-party auth provider "
            "(e.g. eye-move.nl / mijnklantdossier.nl) that also serves other, "
            "unrelated rental sites with DIFFERENT accounts — credentials are "
            "stored per originating listing site, not per shared provider. If "
            "the current login page's own domain has no stored credential, "
            "retry this tool with THIS listing's original domain (from the "
            "'Apply at this URL' line at the top of your task) instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "site": {
                    "type": "string",
                    "description": "Domain or full URL of the login page, e.g. 'ikwilhuren.nu'.",
                },
            },
            "required": ["site"],
        },
    },
}

# Local (non-MCP) fallback tools for when the Playwright MCP's accessibility-
# tree snapshot doesn't show something known to be on the page -- seen
# repeatedly on real listings: an HTML dialog/overlay built without proper
# ARIA roles never gets a browser_snapshot ref, so browser_click can't target
# it and browser_handle_dialog doesn't apply (that's for native JS dialogs,
# not in-page HTML). These query the raw DOM / click by visible text instead
# of the accessibility tree -- narrow, fixed operations, NOT arbitrary JS
# (BLOCKED_TOOLS below still applies).
DOM_SCAN_TOOL = {
    "type": "function",
    "function": {
        "name": "dom_scan",
        "description": (
            "FALLBACK ONLY. Raw-DOM page report (title/url/text + every "
            "button, link, and form field found by direct DOM query) -- NOT "
            "the accessibility tree browser_snapshot uses. Use this ONLY when "
            "you know something is on the page (e.g. you just clicked a "
            "button that should open a dialog/modal) but browser_snapshot "
            "doesn't show it. Waits briefly first so a just-opened dialog has "
            "time to render. Do NOT use this as your primary way to read the "
            "page -- prefer browser_snapshot; this is slower and has no refs, "
            "only visible text."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}
CLICK_BY_TEXT_TOOL = {
    "type": "function",
    "function": {
        "name": "click_by_text",
        "description": (
            "FALLBACK ONLY. Click the first element whose VISIBLE TEXT "
            "matches (not a browser_snapshot ref). Use this ONLY when "
            "dom_scan shows an element you need to click (e.g. inside a "
            "dialog invisible to browser_snapshot) but it has no ref you can "
            "pass to browser_click. Do NOT use this instead of browser_click "
            "for anything a snapshot ref already covers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Visible text of the element to click, e.g. 'Ja, ik ga akkoord'.",
                },
            },
            "required": ["text"],
        },
    },
}
FILL_BY_LABEL_TOOL = {
    "type": "function",
    "function": {
        "name": "fill_by_label",
        "description": (
            "FALLBACK ONLY. Type into the text/email/tel/textarea input "
            "associated with the given <label> text, bypassing the "
            "accessibility-tree ref system. Use this ONLY when dom_scan shows "
            "a form field inside a dialog invisible to browser_snapshot -- "
            "such a field has no ref, so browser_type/browser_fill_form "
            "cannot reach it at all. Do NOT use this instead of "
            "browser_type/browser_fill_form for anything a snapshot ref "
            "already covers."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Visible label text of the field, e.g. 'Voornaam'.",
                },
                "value": {
                    "type": "string",
                    "description": "Text to type into that field.",
                },
            },
            "required": ["label", "value"],
        },
    },
}
SELECT_OPTION_BY_LABEL_TOOL = {
    "type": "function",
    "function": {
        "name": "select_option_by_label",
        "description": (
            "FALLBACK ONLY. Operate a custom (non-<select>) dropdown inside a "
            "dialog invisible to browser_snapshot: opens the dropdown "
            "associated with the given label, then clicks the option matching "
            "the given visible text. Use this ONLY for a dropdown dom_scan "
            "shows with no ref -- e.g. one where the toggle control has no "
            "text of its own (an icon only), so click_by_text can't target it "
            "either. Do NOT use this for a normal <select> or any dropdown a "
            "snapshot ref already covers -- use browser_select_option there."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Visible label text of the dropdown field, e.g. 'Soort inkomen'.",
                },
                "option": {
                    "type": "string",
                    "description": "Visible text of the option to select, e.g. 'Loondienst'.",
                },
            },
            "required": ["label", "option"],
        },
    },
}

# Playwright MCP tools we never want the model to use (raw JS = token bleed).
BLOCKED_TOOLS = {"browser_evaluate", "browser_run_code_unsafe"}
