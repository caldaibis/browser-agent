# Stale page dumps in history = quadratic input tokens

(from AGENTS.md, verbatim; see git history for context)

**Stale page dumps in history = quadratic input tokens.** Every
`browser_snapshot`/`browser_navigate`/`dom_scan` result is ~7k tokens (they
are already clamped at 20k chars), and until 02-07-2026 every one of them
stayed in `messages` for the rest of the run — re-sent to the API on every
later turn. Measured on the worst Hof van Oslo transcript
(20260701_144029, 60 turns): context grew 7.7k → 188k tokens, 6.12M
cumulative prompt tokens for ONE run (the dashboard's 5–6M-token
`incomplete` rows). The model only ever acts on the newest snapshot, so
`_prune_stale_page_dumps` (browser_agent.py) now stubs all but the newest
2 large tool results in place each turn (thresholds via
`APPLY_PRUNE_MIN_CHARS`/`APPLY_PRUNE_KEEP_RECENT`). Each stub invalidates
DeepSeek's prefix cache from that message onward, but the stub lands near
the tail so the one-off miss re-read is far smaller than carrying ~7k
extra tokens on every remaining turn.
