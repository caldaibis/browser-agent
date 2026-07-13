# Architecture

This is the current-system map. For mandatory coding constraints, read
[`../AGENTS.md`](../AGENTS.md); for task-specific commands and tests, read
[`development.md`](development.md).

## Runtime topology

One VPS runs several processes around one persistent browser profile:

```text
                       +-----------------------------+
Gmail API ------------>| orchestrator.service        |
                       |  extract -> filter -> apply  |
                       +--------------+--------------+
                                      |
                                      v
 +----------------+       CDP :9222 + browser_lock       +------------------+
 | browser-host   |<------------------------------------>| agent-browser MCP|
 | Chromium/profile|                                      | apply loop       |
 +----------------+                                       +------------------+
          ^                                                        |
          | session probes / diagnostics                           v
 +--------+---------+      +------------------+             SQLite + logs
 | healthcheck.timer |      | dashboard.service|<-------------------------+
 +-------------------+      +------------------+                          |
                                                                         |
 non-terminal apply -----------------------------------------------------+
     -> durable queue -> self-improvement-worker.timer
        -> isolated worktree -> verify -> push or pending patch
```

The browser host owns Chromium. Every other component attaches to it. Browser
access that can overlap is serialized by `src/browser_lock.py`.

## Listing lifecycle

1. `src/gmail_watch.py` polls unread Stekkies and Huurwoningen alerts and emits
   a typed `GmailListingEvent`.
2. `src/orchestrator.py` coordinates a listing. A Stekkies URL is resolved by
   `src/stekkies.py`; a direct Huurwoningen source can enter without that hop.
3. The orchestrator checks canonical/raw dedup keys from `src/models.py` and
   `src/dedup.py` against the SQLite store.
4. `src/apply.py` performs deterministic vetoes: rent, optional bedroom policy,
   paid gates, and session preparation. Cheap listing context and site memory
   enrich the prompt fail-open.
5. A site fast path may complete a known deterministic flow. Otherwise
   `src/browser_agent/loop.py` runs the DeepSeek tool-calling loop through the
   normalized adapter in `src/browser_agent/transport.py`.
   `src/apply_sessions.py` records the run's live phase and transcript identity
   so the dashboard can show resolving, browser-wait, and agent execution in
   real time. The transcript itself remains the append-only source of truth.
6. The loop returns `AgentResult(rc, outcome, summary, resolved_url)`. The
   resolved URL matters because an aggregator can lead to a previously seen
   listing under a different source URL.
7. The orchestrator records the processed listing and all identities in
   SQLite, writes redacted operational evidence, marks or preserves the email
   according to outcome, and notifies the user.
8. Site playbooks are distilled from the redacted transcript. A configured
   non-terminal outcome is queued for offline self-improvement.

There is no automatic apply retry. `no_credit` is the only completed agent run
that does not consume the listing.

## Apply-agent boundary

The model never receives the 150+ upstream agent-browser tools directly.

```text
apply prompt
  -> browser_agent.loop
     -> transport's normalized browser_* tools
        -> pinned agent-browser MCP
           -> shared Chromium
     -> five local tools (four narrow DOM fallbacks + aggregator_hop)
     -> deterministic guards (payment, repetition, observation use, pruning)
  -> AgentResult
```

`src/agent_tools.py` declares local tool schemas. `transport.py` converts MCP
schemas to the model surface, normalizes safe malformed scalar arguments, limits
uploads to `DOCS_DIR`, marks page content untrusted, and owns wedged-child
teardown. `deploy/agent-browser-action-policy.json` independently denies raw
evaluation, browser administration, state/network mutation, downloads, and
other unused capabilities.

The local DOM fallbacks in `src/browser_dom_tools.py` are fixed operations, not
a generic escape hatch. When an open `<dialog>` exists they scope there first,
because real sites reuse ids across hidden dialogs.

## Self-improvement boundary

`src/self_improvement_agent.py` is the public facade and control plane. Leaf
modules live in `src/self_improvement/`.

1. Failed applies and failed session repairs create redacted incident context.
2. `src/incident_store.py` fingerprints the weakness. Recent identical
   incidents are recorded but do not start another expensive agent run.
3. `src/self_improvement_queue.py` persists a job. A systemd timer invokes
   `src/self_improvement_worker.py` under one global flock.
4. A read-only diagnosis phase must submit an authoritative `DIAGNOSIS_JSON`.
   Only a `fix` verdict gets a patch phase.
5. The patch phase writes only inside a throwaway worktree based on fetched
   `origin/main`, runs `just check`, and uses the dedicated commit/push tool.
6. A fast-forward-safe and allowed fix can push to main, which triggers CI/CD.
   Otherwise it pushes a review branch. If all pushes fail, a `git am` patch is
   saved in `state/pending_patches/` and attached to the alert.

The Claude Agent SDK talks to a loopback LiteLLM proxy backed by
`deepseek-v4-pro`. Claude-specific `thinking`, `effort`, and structured-output
parameters are intentionally absent on this path. SDK-reported dollars are not
trusted; `src/self_improvement/cost.py` estimates cost from raw token usage.

## Data ownership

| Data | Owner | Persistence | Notes |
|---|---|---|---|
| Processed listings and dedup keys | `src/store.py` | `state/store.db` | SQLite/WAL, authoritative |
| Incidents and attempts | `src/store.py`, `src/incident_store.py` | `state/store.db` | Fingerprints collapse repeated weaknesses |
| Runtime settings | `src/settings.py` | environment | Typed parsing/defaults; `just settings` prints redacted values |
| Browser profile and auth vault | browser host / agent-browser | `state/` | Private, shared, never committed |
| Known external gates | `src/known_gates.py` | `state/known_gates.json` | Runtime-editable prevention lever |
| Site playbooks | `src/site_playbooks.py` | `state/site_playbooks/` | Durable mechanics, no listing facts or personal data |
| Mail/poll/activity evidence | orchestrator/eventlog | `logs/*.jsonl`, `logs/activity.log` | Append-only operational logs |
| Apply transcripts | `src/apply.py` | `logs/transcripts/` | May contain secrets before dashboard redaction |
| Live apply-session state | `src/apply_sessions.py` | `state/apply_sessions/` | Current phase, process liveness, listing identity, and transcript pointer |
| Structured trajectories | `src/self_improvement_harness.py` | `logs/trajectories/` | Per-turn machine-readable evidence |
| Pending verified fixes | self-improvement facade | `state/pending_patches/` | Recovery when every push fails |
| Application documents | user / apply prompt | `documents/` or `DOCS_DIR` | Personal data, gitignored |

State answers “what the system currently knows”; logs answer “what happened.”
Do not move append-only forensic evidence into the state store or recreate
authoritative state as parallel JSONL.

## Core contracts

### Domain model

`Listing` is the pipeline currency. `source_url` is required; all enrichment is
optional. `Listing.from_json()` accepts historical dictionaries and
`to_json()` is the compatibility boundary. `ProcessedRecord.keys()` and
`Listing.dedup_keys()` are the only multi-identity derivations.

### Outcomes

`src/browser_agent/result.py` owns valid apply outcomes and return-code parsing.
Outcome consumers include orchestrator mail handling, notifications,
self-improvement triggers, digest/funnel metrics, and dashboard filters. Adding
or renaming one is therefore a cross-cutting schema change.

### Settings

`src/settings.py` owns runtime environment names, compatibility aliases,
parsing, validation, and defaults. The applicant's `APPLICANT_*` identity fields
are the deliberate exception and remain in `src/applicant_profile.py`. Some
modules bind constants at import time for backward-compatible test seams;
callers that require fresh environment values call `settings()` at use time.

### Redaction and time

`src/redaction.py` owns secret removal. `src/eventlog.py` applies it before
write and emits UTC timestamps. Readers accept historical timestamp shapes
through shared parsing helpers.

### Failure policy

The system is deliberately asymmetric:

- Fail closed: duplicate submission, payment, unsafe browser capability,
  unauthorized self-improvement writes, and missing verification.
- Fail open: listing-context enrichment, playbook distillation, notification
  delivery, trajectory recording, and self-improvement scheduling. These
  helpers may report errors but must not rewrite the apply verdict.

## Module map

### Intake and orchestration

- `gmail_watch.py`: Gmail OAuth, MIME/QP decoding, provider-specific events.
- `stekkies.py`: deterministic extraction from a Stekkies page over shared CDP.
- `orchestrator.py`: lifecycle owner, dedup checks, persistence, mail handling,
  notifications, and self-improvement enqueue.
- `apply.py`: apply-stage facade and deterministic pre-flight policy.
- `apply_sessions.py`: cross-process live-run identity, phase, and liveness state.
- `listing_context.py`: cheap fail-open detail-page/JSON-LD enrichment.
- `models.py`, `dedup.py`, `store.py`: schema, identity, and state.

### Browser execution

- `browser_host.py`: persistent Chromium process/profile.
- `browser_lock.py`: cross-process serialization and holder diagnostics.
- `agent_browser_runtime.py`: pinned-runtime startup checks.
- `browser_agent/loop.py`: model loop and orchestration.
- `browser_agent/transport.py`: MCP normalization, dispatch, secure login,
  upload policy, logging, and teardown watchdog.
- `browser_agent/guards.py`: deterministic loop/payment/token guards.
- `browser_agent/result.py`: result and outcome contract.
- `browser_dom_tools.py`: narrow ARIA-blind DOM fallbacks.
- `site_fastpaths.py`: deterministic site-specific shortcuts.
- `agent_tools.py`: local model-tool schemas.

### Policy and memory

- `prompts/apply_prompt.py`: autonomous apply policy and document ordering.
- `message_template.py`: reference message content.
- `applicant_profile.py`: applicant facts from `APPLICANT_*` environment.
- `rent_policy.py`, `bedroom_policy.py`: deterministic eligibility vetoes.
- `credentials.py`, `import_passwords.py`: domain credentials and import.
- `session_keeper.py`: proactive per-domain session probing/repair.
- `site_playbooks.py`: learned site mechanics.
- `known_gates.py`: diagnosed external constraints.

### Evidence, repair, and observability

- `eventlog.py`, `redaction.py`: shared safe writes.
- `self_improvement_harness.py`: trajectories, classifiers, offline fixtures.
- `incident_store.py`: incident fingerprint/dedup semantics.
- `self_improvement_queue.py`, `self_improvement_worker.py`: durable serialized
  execution.
- `self_improvement_agent.py`: diagnosis/patch control plane and public API.
- `self_improvement/`: prompts, worktrees, browser diagnostics, cost, helpers.
- `healthcheck.py`, `digest.py`, `notify.py`, `push_notify.py`: alerts and
  operational summaries.
- `dashboard/`: read-mostly operator UI, live transcript SSE, and a small set
  of explicit POST actions. Retry creates a session before launch and redirects
  directly to its replay-plus-live-tail view.

### Auxiliary and compatibility entry points

- `reauth.py` and `import_passwords.py`: explicit credential-maintenance tools.
- `matches.py`: deterministic read-only listing enumeration over shared CDP.
- `browser_token_metrics.py` and `llm_pricing.py`: offline usage analysis and
  shared price tables.
- `recovery_agent.py`: backward-compatible import shim for the renamed
  self-improvement facade; new code imports `self_improvement_agent.py`.
- `capture_form.py` and `login_setup.py`: legacy standalone headed-browser
  utilities, not runtime components. They create their own persistent contexts;
  do not run them alongside `browser-host` or use them as patterns for new code.

## Terms

- **Stekkies URL:** notifier/aggregator redirect used to discover a listing.
- **Source URL:** input URL where application work starts; it may itself be an
  aggregator.
- **Resolved URL:** actual destination reached during the run and an additional
  dedup identity.
- **Gate:** external condition such as payment, account cap, or eligibility.
- **Playbook:** per-domain durable mechanics distilled from prior runs.
- **Trajectory:** redacted structured per-turn apply evidence.
- **Incident:** deterministic fingerprint grouping repeated failures.
- **Fast path:** deterministic known-site operation attempted before the LLM
  loop, without changing the final `AgentResult` contract.
