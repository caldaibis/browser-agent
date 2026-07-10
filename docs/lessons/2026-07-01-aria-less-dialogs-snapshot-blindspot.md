# Accessibility-tree snapshots miss ARIA-less dialogs

(from AGENTS.md, verbatim; see git history for context)

**Accessibility-tree snapshots miss dialogs built without proper ARIA
roles.** Seen repeatedly on real listings, most recently Hof van Oslo via
REBO Groep (01-07-2026): a "credit check" consent dialog opened and
intercepted all clicks, but `browser_snapshot` never showed it (no
`dialog`/`button` role on its markup) — the agent burned ~18 of 60 turns
trying screenshots, `boxes` snapshots, and console/network inspection
before giving up. `browser_handle_dialog` doesn't help either — that's for
native JS `alert`/`confirm`, not in-page HTML. Fix: `dom_scan`/
`click_by_text` (raw DOM query + click-by-visible-text, `src/
browser_dom_tools.py`) as a narrow, explicitly-scoped fallback — not a
reopening of raw JS. Also seen in the same transcript: ~29 of 60 turns
were `browser_snapshot` calls, each after a *different* click, so neither
the prompt's own "don't re-snapshot every click" guidance nor the
exact/short-cycle repeat guard caught it (the repeated element is the call
*type*, not its arguments) — `_should_nudge_snapshot_overuse` adds a
one-shot code-level nudge for this specific pattern.
