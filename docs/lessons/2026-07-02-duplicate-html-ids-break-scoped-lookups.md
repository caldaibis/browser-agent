# Duplicate HTML ids break scoped lookups, not just accessibility

(from AGENTS.md, verbatim; see git history for context)

**Duplicate HTML ids break scoped lookups, not just accessibility.**
REBO Groep reuses `id="first_name"`/`id="email"`/etc across three different
`<dialog>` elements on one page (viewing request, brochure download, email
upsell) — invalid HTML, but real. `getElementById` and Playwright's
`get_by_label` (which resolves a `<label for=id>` via a similar document-wide
lookup) both silently resolve to whichever hidden dialog comes first in DOM
order, not the open one: a fill sees a 0×0 bounding box and times out with no
hint why. `get_by_text`, by contrast, verifiably scopes correctly to a
Locator's own subtree. Fix: every raw-DOM tool scopes to the currently open
`<dialog>` first (`dialog_scope` in `browser_dom_tools.py`), and
`fill_by_label`/`select_option_by_label` find inputs/dropdowns by walking up
from a text-matched `<label>` rather than trusting `for=id` resolution.
