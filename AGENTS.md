# Coding-agent guide

This repository is the Stekkies rental auto-responder. It watches Gmail for a
new listing, resolves the Stekkies or aggregator link, and uses a browser agent
to apply on the source site. The final action is a real submission: there is no
dry-run confirmation step.

## Start here

Read only the context needed for the task, in this order:

1. This file for repository-wide constraints.
2. [`docs/architecture.md`](docs/architecture.md) for processes, data flow,
   ownership, and the module map.
3. [`docs/development.md`](docs/development.md) for task routing, commands, and
   validation.
4. The nearest nested `AGENTS.md` for files you will edit.
5. The linked incident lesson before changing code protected by a hard-won
   rule.

[`docs/README.md`](docs/README.md) is the documentation index. Do not read the
roadmap, every incident, or deployment guide by default.

## Non-negotiable safety rules

- The apply path submits autonomously. A local smoke run can send a real rental
  application, upload personal documents, or trigger site actions. Do not run
  `just once`, `just watch`, the live dashboard retry action, or production
  browser flows merely to validate a code change.
- One persistent Chromium instance is shared over CDP port 9222. The extractor,
  apply agent, session keeper, health probes, and browser diagnostics must
  attach to it under `browser_lock`; do not launch a competing browser backend.
- `agent-browser` is the sole apply browser backend. Its version is pinned in
  `deploy/agent-browser.version`; its daemon policy is deny-by-default. Do not
  restore raw JavaScript evaluation or a second browser tool contract.
- `state/`, `logs/`, `.env`, and all files in `documents/` except its README are
  local/production data. They can contain credentials, prompts, transcripts,
  tokens, and personal documents. Never print, stage, or commit them.
- Runtime VPS configuration is `state/agent.env`, loaded by systemd. `.env` is
  only for local `just` variables. Do not `source state/agent.env`: values may
  contain spaces or parentheses that systemd accepts but a shell does not.
- Every completed apply run consumes the listing, regardless of outcome, except
  `no_credit` (`rc=126`). Never turn HTTP 402 into a listing verdict or mark its
  mail read.
- One listing gets one automatic attempt. Do not add automatic retries; they
  repeat the same expensive run without carrying browser-agent reasoning over.
- Already applied means stop. Saved or pre-filled form data is not proof; site
  wording such as “Aanvraag wijzigen”, “Reactie intrekken”, “je hebt
  gereageerd”, or “Doorgaan met gesprek” is.
- Paid registration, account caps, regional registration, delayed access, and
  eligibility failures are real external gates. Report or record them; do not
  bypass payment or registration controls.
- Redact before persistence, not just at presentation time. `src/redaction.py`
  owns redaction and `src/eventlog.py` owns persisted event writes.

## System at a glance

```text
Gmail alert
  -> orchestrator
  -> deterministic Stekkies/source extraction
  -> policy + duplicate pre-flight
  -> shared-browser apply agent
  -> SQLite outcome + redacted logs + notification
                         |
                         +-> non-terminal failure queue
                             -> isolated self-improvement worktree
```

Canonical ownership:

| Concern | Source of truth |
|---|---|
| Runtime settings and defaults | `src/settings.py` (`just settings`) |
| Paths and fixed URLs | `src/config.py` |
| Listing and processed-record schemas | `src/models.py` |
| Listing identity/canonicalization | `Listing.dedup_keys()`, `ProcessedRecord.keys()`, `src/dedup.py` |
| Durable processed/incident state | `src/store.py` → `state/store.db` |
| Event writes and timestamps | `src/eventlog.py` |
| Redaction | `src/redaction.py` |
| Apply prompt policy | `src/prompts/apply_prompt.py` |
| Reference application wording | `src/message_template.py` |
| Apply outcomes | `src/browser_agent/result.py` |
| Browser tool exposure | `src/browser_agent/transport.py`, `src/agent_tools.py` |
| Browser daemon policy | `deploy/agent-browser-action-policy.json` |
| Per-domain learned mechanics | `src/site_playbooks.py` → `state/site_playbooks/` |
| Diagnosed external gates | `src/known_gates.py` → `state/known_gates.json` |

Subsystem guides:

- General Python/data boundaries: [`src/AGENTS.md`](src/AGENTS.md)
- Apply browser loop: [`src/browser_agent/AGENTS.md`](src/browser_agent/AGENTS.md)
- Prompt policy: [`src/prompts/AGENTS.md`](src/prompts/AGENTS.md)
- Self-improvement: [`src/self_improvement/AGENTS.md`](src/self_improvement/AGENTS.md)
- Dashboard: [`src/dashboard/AGENTS.md`](src/dashboard/AGENTS.md)
- Tests and fixtures: [`tests/AGENTS.md`](tests/AGENTS.md)
- Deployment/systemd: [`deploy/AGENTS.md`](deploy/AGENTS.md)

## Working conventions

- Python 3.12 is managed by `uv`. Use `uv run`; do not manually activate the
  virtualenv.
- Use `just` recipes for established workflows. `just check` is the single
  required quality gate and is also the autonomous self-improvement verifier.
- Run modules as packages: `uv run python -m src.<module>`.
- Runtime settings belong in the frozen `Settings` dataclass and
  `load_settings()`; modules bind from `settings()`. The deliberate exception is
  the applicant's `APPLICANT_*` profile, owned by `src/applicant_profile.py`.
  Passing a copied environment to a subprocess is not a new settings source.
- Carry a typed `Listing` through the core pipeline. Convert at explicit
  compatibility or persistence boundaries with `from_json()` / `to_json()`.
- SQLite is authoritative for processed listings, dedup keys, and incidents.
  Append-only operational evidence remains in JSONL/transcript files.
- Fail-open enrichments must never fail an apply: listing-context fetches,
  playbook updates, notifications, and self-improvement enqueueing are examples.
  Safety vetoes, duplicate prevention, payment prevention, and browser policy
  remain fail-closed.
- Keep imports and public facades compatible unless a task explicitly changes
  them. Tests patch seams such as `src.browser_agent.loop`, not only package
  re-exports.
- Preserve a dirty worktree. Do not overwrite unrelated user changes or use
  destructive Git commands.

## Change workflow

1. Inspect `git status --short` and the relevant call sites/tests.
2. Read the closest subsystem guide and any incident it names.
3. Make the smallest coherent change at the canonical owner above; do not add
   a second source of truth.
4. Run focused tests from the routing table in
   [`docs/development.md`](docs/development.md), then run `just check`.
5. Review the diff for secrets, generated runtime data, prompt changes, policy
   widening, outcome changes, and stale documentation references.

For documentation-only changes, `just docs-check` is the focused check, but
`just check` remains the final gate.

## Hard-won constraints

Read the linked lesson before touching the related path.

- Alerting must not share a failure mode with the monitored component. Push is
  attempted before email; health checks include unit liveness and a dead-man
  ping. [`2026-07-04`](docs/lessons/2026-07-04-alerting-shared-failure-mode.md)
- Threads waiting on file locks can starve asyncio DNS. Preserve the large poll
  executor, bounded tier-3 lock wait, and staggered startup.
  [`2026-07-07`](docs/lessons/2026-07-07-asyncio-executor-dns-starvation.md)
- `asyncio.wait_for` cannot unwind a stuck MCP teardown. Preserve the descendant
  watchdog and browser-lock holder diagnostics.
  [`2026-07-03`](docs/lessons/2026-07-03-hung-mcp-teardown-watchdog.md)
- HTTP 402 is `no_credit`, not an apply result.
  [`2026-07-05`](docs/lessons/2026-07-05-no-credit-is-not-a-verdict.md)
- Apply reasoning is off by default; an empty length-truncated model turn is
  retried and token/finish metadata stays observable.
  [`2026-06-29`](docs/lessons/2026-06-29-reasoning-truncation-silent-stall.md)
- Accessibility snapshots can miss ARIA-less dialogs. Use only the narrow,
  open-dialog-scoped DOM fallbacks; do not reopen arbitrary evaluation.
  [`2026-07-01`](docs/lessons/2026-07-01-aria-less-dialogs-snapshot-blindspot.md)
- Duplicate HTML ids require open-dialog scoping and label-relative lookup.
  [`2026-07-02`](docs/lessons/2026-07-02-duplicate-html-ids-break-scoped-lookups.md)
- Custom dropdown option buttons can submit forms unless guarded.
  [`2026-07-02`](docs/lessons/2026-07-02-dropdown-options-default-to-submit.md)
- Ref-less dialogs need the local fallback tool chain and current-tab
  detection. [`Hof van Oslo`](docs/lessons/2026-07-02-hof-van-oslo-resolution.md)
- One listing may have several URL shapes; preserve raw and canonical keys.
  [`Kaatstraat`](docs/lessons/2026-07-02-kaatstraat-one-listing-many-url-shapes.md)
- The resolved destination is also a dedup key, including during an active run.
  [`cross-source dedup`](docs/lessons/2026-07-02-cross-source-dedup-gap.md)
- Keep stale large page dumps pruned in place or token input grows
  quadratically. [`2026-07-02`](docs/lessons/2026-07-02-stale-page-dumps-quadratic-tokens.md)
- Grace turns and the deterministic cookie sweep protect turn-budget progress.
  [`2026-07-02`](docs/lessons/2026-07-02-eligibility-gates-readable-at-poll-time.md)
- Self-improvement diagnosis authority, tool availability, worktree isolation,
  and pending-patch recovery are separate control-plane guarantees.
  [`2026-07-10`](docs/lessons/2026-07-10-self-improvement-control-plane-failures.md)

Additional fixed facts: the apply model defaults to `deepseek-v4-pro`; do not
restore `gemini-3.5-flash`. Gmail listing bodies are quoted-printable and the
Stekkies redirect id is an alphanumeric hash. Document priority is owned by
`_classify()` in `src/prompts/apply_prompt.py`; keep expired contracts out and
bank statements trimmed.

## Documentation contract

- `AGENTS.md` contains global constraints, not subsystem implementation prose.
- Nested `AGENTS.md` files contain only local ownership, invariants, and focused
  verification.
- `docs/architecture.md` describes the current system; update it in the same
  change when a boundary or flow changes.
- Incident history goes in `docs/lessons/YYYY-MM-DD-<slug>.md`; add its standing
  rule here or in the relevant nested guide.
- Future ideas go in `docs/planned-features.md`; completed migration history
  remains in `docs/engineering-roadmap.md`.
- Use relative Markdown links for repository navigation so `just docs-check`
  can catch moved or missing targets.
