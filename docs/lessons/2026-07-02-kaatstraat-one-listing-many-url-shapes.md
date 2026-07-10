# One site, one listing, several URL shapes

(from AGENTS.md, verbatim; see git history for context)

**One site, one listing, several URL shapes — path-based keying can't
connect them.** Kaatstraat (02-07-2026): the Huurwoningen alert mail
deep-links `/frontend/listing/<full-uuid>/?alt=...` while Stekkies extracts
(and the poller discovers) the site page `/huren/<city>/<uuid-first-8-hex>/
<street-slug>/`. Same listing, two canonical keys → the pre-flight duplicate
check matched neither and TWO full agent runs (~$0.07) were spent only for
the mid-run guard to stop each at the real landlord site
(eenhoornmanagement.nl — huurwoningen.nl is often just the shop window).
Fixes: (1) `dedup.canonical_url` collapses both huurwoningen shapes to a
synthetic per-listing key (`https://huurwoningen.nl/listing/<hex8>`, see
`_site_listing_key` — extend it when another site shows the same disease);
backward compatible because every reader re-canonicalizes stored keys at
load time. (2) `orchestrator._processed_keys` now also reads
`resolved_url`, so a mail pointing straight at a landlord site an earlier
run only reached mid-flight is caught pre-flight too. (3) a deterministic
prevention is deliberately visible: `skipped_duplicate` rows land in
`mail_summary.jsonl` with a "Prevented by the deterministic duplicate
guard..." message and show in the dashboard's submissions list (no status
filter hides them) — prevented spend should be observable, not silent.
