# Production lessons

Each dated file preserves evidence from a production incident. Current coding
rules live in the root or nearest subsystem `AGENTS.md`; read a lesson when a
change touches the protected mechanism.

| Incident | Standing rule | Primary area |
|---|---|---|
| [`2026-06-29 reasoning truncation`](2026-06-29-reasoning-truncation-silent-stall.md) | Empty length-truncated reasoning is retried, not accepted | Browser loop/model usage |
| [`2026-07-01 ARIA-less dialogs`](2026-07-01-aria-less-dialogs-snapshot-blindspot.md) | Keep narrow DOM fallbacks and observation-overuse nudging | Browser DOM tools |
| [`2026-07-02 Hof van Oslo`](2026-07-02-hof-van-oslo-resolution.md) | Ref-less dialogs need current-tab and label/text fallbacks | Browser loop/tools |
| [`2026-07-02 duplicate ids`](2026-07-02-duplicate-html-ids-break-scoped-lookups.md) | Scope DOM lookup to the open dialog and walk from labels | Browser DOM tools |
| [`2026-07-02 dropdown submit`](2026-07-02-dropdown-options-default-to-submit.md) | Guard forms before clicking custom options | Browser DOM tools |
| [`2026-07-02 URL shapes`](2026-07-02-kaatstraat-one-listing-many-url-shapes.md) | Preserve raw and canonical listing identities | Models/dedup/store |
| [`2026-07-02 cross-source dedup`](2026-07-02-cross-source-dedup-gap.md) | Persist and actively check resolved destination URLs | Orchestrator/browser loop |
| [`2026-07-02 stale page dumps`](2026-07-02-stale-page-dumps-quadratic-tokens.md) | Prune old large observations in place | Browser guards/loop |
| [`2026-07-02 eligibility and turn budget`](2026-07-02-eligibility-gates-readable-at-poll-time.md) | Preserve eligibility warnings, cookie sweep, and evidenced grace turns | Prompt/apply loop |
| [`2026-07-03 hung MCP teardown`](2026-07-03-hung-mcp-teardown-watchdog.md) | Keep process watchdog and browser-lock holder evidence | Transport/browser lock |
| [`2026-07-04 shared alert failure`](2026-07-04-alerting-shared-failure-mode.md) | Alert channels and liveness monitoring must fail independently | Notify/health/deploy |
| [`2026-07-05 no credit`](2026-07-05-no-credit-is-not-a-verdict.md) | HTTP 402 never consumes a listing | Result/orchestrator |
| [`2026-07-07 executor starvation`](2026-07-07-asyncio-executor-dns-starvation.md) | Bound lock waits and preserve executor headroom for DNS | Polling/browser lock |
| [`2026-07-10 SI control plane`](2026-07-10-self-improvement-control-plane-failures.md) | Separate diagnosis authority, tool availability, worktree writes, and recovery | Self-improvement |

Add a new incident as `YYYY-MM-DD-<slug>.md`, link it here, and put the durable
rule in the closest agent guide. Keep listing facts and personal data redacted.
