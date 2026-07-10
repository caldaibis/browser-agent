# Cross-source dedup gap: the same listing, two different keys

(from AGENTS.md, verbatim; see git history for context)

**Cross-source dedup gap: the same listing, two different keys.** The
Stekkies-mail path records the final external `source_url` (already resolved
by Stekkies' own extraction); the poller records whatever URL it discovered
the listing at, which for an aggregator (huurwoningen.nl) is a DIFFERENT URL
than the real destination reached only after clicking through in-page
redirect dialogs. Neither recognized the other's key as the same real-world
listing. This is why Hof van Oslo got stuck in an endless poller retry loop
in the first place (see above) AND why a manual retest of the fixed agent on
02-07-2026 submitted a real, duplicate second application to REBO — the
poller-triggered run had no way to know a Stekkies-triggered run had already
succeeded under a different URL. Fixed two ways: (1) `apply.py`/
`browser_agent.py` now capture `AgentResult.resolved_url` — the actual
external destination an apply run reaches mid-flight — and persist it as an
extra dedup key in `processed_listings.jsonl` (`orchestrator.py`,
`poller/watcher.py`); `dedup.known_processed_urls()` reads all of
`source_url`/`stekkies_url`/`resolved_url` across both the poller's and the
orchestrator's records. (2) since an aggregator's real destination can't be
resolved before opening the browser (it's in-page JS, not an HTTP redirect
`fetch.py` could follow), `browser_agent.py`'s `_run()` also checks the
*current* tab's URL against that same set once per turn — the earliest point
a duplicate can actually be caught — and stops immediately with
`already_applied` instead of re-filling/resubmitting a form the target site
itself gives no "already applied" signal for.
