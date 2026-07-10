# HTTP 402 (out of credit) is not a verdict on the listing

(from AGENTS.md, verbatim; see git history for context)

**HTTP 402 (out of credit) is not a verdict on the listing.** Every apply
outcome used to consume the listing (one-attempt rule) — so during a credit
outage every listing that dropped was burned forever as "error". outcome
`no_credit` (rc=126) is now a third carve-out alongside `yielded` and the
browser-lock timeout: poller releases the claim, orchestrator leaves the
mail unread, both alert (rate-limited via `notify.send_alert_dedup`).
