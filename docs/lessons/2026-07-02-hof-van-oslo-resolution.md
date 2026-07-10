# Hof van Oslo, resolved: driving a ref-less dialog end to end

(from AGENTS.md, verbatim; see git history for context)

**Hof van Oslo, resolved (02-07-2026):** the above dialog-blindspot fix
(`dom_scan`/`click_by_text`) alone wasn't enough — three more real, verified
bugs surfaced only once the actual REBO Groep dialog was driven end to end.
(1) `dom_scan`'s "current page" picked the last-*created* tab, not the one
the Playwright MCP actually had selected — with several tabs open (SSO
popups, an inschrijfportaal tab...) that's silently the wrong tab. Fixed by
asking the MCP's own `browser_tabs` listing (which marks the true current
tab with `(current)`) and passing that URL through as a hint (`current_page`
in `browser_dom_tools.py`). (2) an uncaught `Locator.click` timeout inside
`click_by_text` propagated out and killed the whole MCP session/process —
now caught and returned as a normal (recoverable) tool result. (3) there was
no way to *type* into a ref-less dialog's inputs at all — `dom_scan` can
read, `click_by_text` can only click. Added `fill_by_label` and
`select_option_by_label` (see architecture section above) — the missing
piece that actually let the agent complete the form. Also found: REBO
Groep's page has a button labelled "Inschrijven huuraanbod" that opens a
**paid €34,95/year email-alert subscription**, not an application for the
listing — a real dark pattern, verified by inspecting the dialog's DOM
directly (title: "Schrijf je in voor onze e-mailservice"), not assumed;
`apply.py`'s prompt now warns against it by name. With all of the above,
the same listing went from a 60-turn timeout to a 23-turn real submission.
