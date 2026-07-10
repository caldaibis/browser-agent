# Hard published eligibility gates are readable at poll time

(from AGENTS.md, verbatim; see git history for context)

**Hard published eligibility gates are readable at poll time.** Full agent
runs were spent opening the browser just to read "ALLEEN BESCHIKBAAR VOOR
STUDENTEN" (huurportaal, 02-07-2026, twice in one day).
`filters.hard_exclusion` vetoes students-only/seniors-only/short-stay
listings deterministically from the title+description (sentence-scoped so
"geen studenten" — students *excluded*, fine for us — never triggers), the
judge gets the description + matching criteria, and
`browser_agent`'s turn budget grants one `APPLY_GRACE_TURNS` extension when
the run is demonstrably mid-form (two runs died at turn 60 one dropdown from
submitting — under one-attempt, that consumed the listings forever). A
deterministic cookie-banner sweep (`dismiss_cookie_banner`) runs after every
navigation so consent overlays never cost LLM turns.
