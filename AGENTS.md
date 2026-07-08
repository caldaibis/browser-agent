# AGENTS.md

Stekkies rental auto-responder. Pipeline: Gmail (new Stekkies mail) → extract
listing metadata + external source URL → our browser agent applies on the source
site using the reference message in `src/message_template.py` (logs in, fills
form, uploads docs, submits).

## Architecture
- **One shared browser** (`src/browser_host.py`): persistent Chromium on CDP
  port 9222. The Stekkies extractor and the apply agent both attach over CDP, so
  all logins (Google SSO + rental sites) live in one profile signed into once.
- `src/stekkies.py` — attach over CDP, extract listing/source URL (deterministic).
- `src/browser_agent.py` — our own lightweight agent loop (replaced Hermes):
  AsyncOpenAI (DeepSeek) tool-calling over the **Playwright MCP**
  (`npx @playwright/mcp@latest --cdp-endpoint http://127.0.0.1:9222`) for
  snapshot/click/fill_form/file_upload. Filters raw-JS tools (browser_evaluate,
  browser_run_code_unsafe), has a repeat-action guard + capped nudges, a
  one-shot nudge when `browser_snapshot` dominates the turns so far
  (`_should_nudge_snapshot_overuse` — the exact/short-cycle repeat guard
  can't catch this: the *type* of call repeats, not its arguments, since
  each snapshot follows a *different* click), prunes stale page dumps from
  the conversation each turn (`_prune_stale_page_dumps` — all but the newest
  2 large tool results are stubbed in place, since the model only ever acts
  on the latest snapshot; without this cumulative input tokens grow
  quadratically with turns — see the hard-won lesson below), and returns a
  structured
  `AgentResult(rc, outcome, summary, resolved_url)`. Four local (non-MCP)
  fallback tools (`src/browser_dom_tools.py`, shared with self-improvement's
  own browser diagnostics) give the model a way past HTML dialogs/overlays
  that lack proper ARIA roles and never get a `browser_snapshot` ref:
  `dom_scan` (raw-DOM report), `click_by_text` (click by visible text),
  `fill_by_label` (type into a text/email/tel/textarea input by its `<label>`
  text — there is no other way to reach a ref-less input at all, since
  `browser_type`/`browser_fill_form` need a snapshot ref and `click_by_text`
  only clicks), and `select_option_by_label` (operate a custom dropdown whose
  toggle has no text of its own, so `click_by_text` can't target it — clicks
  the nearest ancestor-with-a-button, then the option text; guards every
  `<form>` against a premature real submit first, since option buttons on
  real sites often default to `type="submit"`). All four are scoped to the
  currently *open* `<dialog>` first when one exists (`dialog_scope`) — sites
  can reuse the same field ids across several hidden dialogs on one page
  (verified: REBO Groep's viewing-request, brochure-download, and
  email-upsell dialogs all use `id="first_name"` etc), so an unscoped
  `getElementById`/`get_by_label`/`get_by_text` silently resolves to a hidden
  duplicate — a 0×0 bounding box for a fill, or a `click_by_text` timeout for
  a click, not an error pointing at the real cause. None of this is arbitrary
  JS: each is one fixed, narrow operation, unlike the still-blocked
  `browser_evaluate`. `_run()` also does a cross-source duplicate check once
  per turn: if the browser's current tab lands on a URL already recorded as
  processed under a *different* URL than this run's input (see
  `poller/dedup.py`'s `known_processed_urls`), it stops immediately with
  `already_applied` instead of re-filling/resubmitting a form the site itself
  gives no "already applied" signal for.
- `src/apply.py` — build the prompt, run the agent, persist a per-run transcript
  to `logs/transcripts/<ts>_<source>_<address>.log`. Apply model:
  `deepseek-v4-pro` (override via `APPLY_MODEL`);
  gemini-3.5-flash was too flaky.
- `src/message_template.py` — reference application message; the agent customizes
  it per listing instead of pasting verbatim.
- `documents/` — your application PDFs/JPG. **Gitignored** (never committed —
  they hold personal data); place your own files here and copy them to the VPS
  out of band. `DOCS_DIR` (config) points here; override with the env var.
- `src/credentials.py` / `import_passwords.py` — per-site logins by domain.
- `src/gmail_watch.py` — poll inbox (5s), extract Stekkies link.
- `src/orchestrator.py` — ties it together (`--once URL` or live watch); logs the
  true `outcome` (submitted / already_applied / not_available / …) and only marks
  a listing processed when the outcome is terminal.
- `src/poller/` — **active site poller** (the "don't wait for Stekkies mail" path;
  design in `docs/poller-plan.md`). Watches source sites directly and feeds the
  same `apply.py`. `watcher.py` runs each enabled site on its own cadence+jitter,
  `fetch.py` does httpx GET + block/challenge detection, `parsers.py` has a
  generic schema.org JSON-LD parser (tier-2 default), `filters.py` is the
  deterministic pre-filter (price/city/surface/room), `judge.py` is the cheap-LLM
  judgment (distance-to-centre + roommates, fail-open), `dedup.py` keys on the
  canonical (tracking-stripped) source URL and cross-checks
  `processed_listings.jsonl` (both `source_url` and `resolved_url` — the
  latter is the real external destination an apply run discovered mid-flight,
  e.g. after in-page redirect dialogs on an aggregator page; needed because
  the poller discovers a listing at whatever URL it found it while the
  Stekkies flow records the final resolved URL — two different keys for the
  same real-world listing otherwise, see `known_processed_urls()`),
  `browser_lock.py` is a cross-process flock so the
  poller's applier and the Stekkies orchestrator never drive the shared browser
  at once (also wired into `apply.apply()`). `registry.py` lists all 26 sites;
  `discover.py` probes which tier each site currently yields. Run: `just poll`,
  `just poll-once <site>`, `just discover`. **One attempt per listing — no
  automatic retries** (02-07-2026, replaced the earlier `MAX_POLLER_ATTEMPTS`
  retry cap): a retry re-runs the identical prompt against the same page at
  full LLM cost — nothing carries over from the failed attempt, so it's the
  same coin flip again (seen: `Hof van Oslo` retried 15+ times over several
  hours on 2026-07-01, several million tokens each, all `incomplete`). Every
  completed agent run marks the listing seen/processed whatever the outcome;
  the orchestrator's mail path records non-terminal outcomes too, for the same
  reason. Two carve-outs that are NOT attempts and don't consume the listing:
  outcome `yielded` (run aborted to hand the browser to a priority mail apply,
  see `src/apply_priority.py` — requeued untouched) and a browser-lock
  `TimeoutError` (agent never ran — claim released for a future poll).
- `src/apply_priority.py` — **mail-apply priority over the poller.** Rentals
  are won by minutes: a mail-triggered apply (Stekkies/Huurwoningen alert —
  competitors were just notified too) must not queue behind a speculative
  poller run holding the shared-browser flock for up to 15 min. The
  orchestrator holds a priority flag (`state/apply_priority.flag`, stale after
  `APPLY_PRIORITY_STALE_SECONDS`) around extraction+apply; the poller's
  applier waits for it before starting, and an in-flight poller run checks it
  once per agent turn (`browser_agent._run`'s `yield_check`, wired via
  `apply(..., yield_to_priority=True)`) and aborts with rc=125 / outcome
  `yielded` so the lock frees within one turn. A yielded listing is requeued
  untouched — it is not an attempt.
- `src/site_playbooks.py` — **per-domain playbooks: persistent memory of how
  each site works.** After every real agent run, one cheap LLM pass per
  touched domain (source + resolved) distills the redacted transcript into
  `state/site_playbooks/<domain>.md` — durable site mechanics only (login
  quirks, where the real apply action is, upsell traps, ref-less dialogs), no
  listing facts, no personal data. `apply.build_prompt` injects the listing
  domain's playbook into the next run on that site, so lessons compound
  instead of every run rediscovering the site from scratch (the whole Hof van
  Oslo saga was exactly that rediscovery). Fail-open everywhere: a playbook
  failure never fails an apply. Env: `PLAYBOOK_MODEL`, `PLAYBOOK_MAX_CHARS`,
  `PLAYBOOK_TIMEOUT_SECONDS`.
- `src/self_improvement_harness.py` — **structured failure evidence** (offline,
  fail-open). `record_trajectory_event` is called from `browser_agent._run` to
  write redacted, typed JSONL per run (`logs/trajectories/`): turn usage, tool
  calls/results, guard firings, final outcome — the machine-readable
  counterpart of the transcript (`APPLY_TRAJECTORY_ENABLED=0` disables).
  `classify_failure` is the deterministic weakness classifier shared by
  incident fingerprinting, `just self-improve-mine` (cluster recent failed
  transcripts into evidence bundles) and `just self-improve-eval` (fixture
  regressions in `tests/fixtures/self_improvement_harness/` that keep this
  file's hard-won lessons executable; also runs inside `just check` via the
  test suite). Deliberately NOT here: autonomous "harness evolution"
  self-patching — that duplicated `self_improvement_agent.py` with weaker
  guardrails and was dropped (07-07-2026); code changes go through the
  guarded agent only.
- `src/incident_store.py` — **cross-run memory: incidents, not episodes.**
  Production data (48 runs to 07-07-2026) showed ~half of all
  self-improvement runs re-diagnosed a failure another run had already worked
  on the same day (the 03-07 browser_lock hang: FIVE full runs in seven
  hours). Every failure gets a deterministic fingerprint
  (`classify_failure` signature, domain-scoped for site-specific classes,
  global for infrastructure classes so cross-listing infra failures collapse
  into one incident); `state/self_improvement/incidents.jsonl` records every
  occurrence and attempt. `improve_after_apply` skips the run when the
  fingerprint already had one within `SELF_IMPROVEMENT_DEDUP_HOURS` (24h
  default — the occurrence is still recorded, so prevented spend stays
  observable) and otherwise injects `attempt_history` into the context so
  run N starts from run N-1's findings.
- `src/known_gates.py` — **a data lever for diagnosed external gates.**
  `state/known_gates.json` holds per-domain gates the self-improvement agent
  records via its `record_known_gate` tool at diagnosis time
  (paid_registration / account_cap / region_registration / delayed_access /
  eligibility, optional `expires_ts` for temporary caps). `apply.apply`
  pre-flight skips paid_registration domains as `payment_required` before
  the browser opens (merged into `_payment_required_reason`);
  `apply.build_prompt` injects the other kinds as KNOWN GATES warnings. A
  diagnosis becomes deterministic prevention with no commit/CI/deploy, and
  is reversible by deleting a JSON entry. (Why: your-house.nl's €25 gate was
  correctly diagnosed twice in one day, but turning that into prevention
  needed a human commit.)
- `src/digest.py` — weekly outcome digest (`just digest`; sent by the
  healthcheck every `DIGEST_INTERVAL_DAYS`, default 7): outcomes by trigger,
  apply-loop guard fire counts (from trajectories), self-improvement actions
  + landing rate, top incident fingerprints, **unlanded pending patches**,
  active known gates. Exists because nobody could tell whether a change
  improved the pipeline without aggregating logs by hand.
- `src/self_improvement_agent.py` — runs after a non-terminal apply outcome
  (blocked/error/incomplete/timeout/not_available/…, see
  `SELF_IMPROVEMENT_OUTCOMES`) **and** after a poller site goes silently
  zero-yield (`improve_poller_zero_yield`, triggered from
  `poller/watcher.py` at the `POLL_ZERO_YIELD_ALERT_POLLS` streak — instead
  of emailing a human to run `just poll-once` and fix the parser, it hands
  the saved sample HTML to the same two-phase engine, which diagnoses the
  broken parser and patches `src/poller/parsers.py`/`registry.py` — or
  disables a now-gated site — verifies and deploys; the old alert email only
  fires if self-improvement is disabled entirely). Both triggers share
  `_run_for_incident` (incident dedup + logging) and dispatch the
  diagnosis/patch prompts on `context["kind"]` (`apply` vs
  `poller_zero_yield`). Drives the **Claude Agent SDK**
  (`claude_agent_sdk.query()` — the same engine behind Claude Code: real
  `Read`/`Edit`/`Bash`/`Grep`/`Glob`). Each run creates a **throwaway git
  worktree** (`_create_worktree`, a sibling dir
  `../browser-agent-self-improvement-worktrees/<ts>` branched off a
  freshly-fetched `origin/main` — never `PROJECT_ROOT` itself, so a run can
  never collide with the live checkout or in-progress human edits) and
  always removes it in a `finally` (`_remove_worktree`), even on timeout.
  **Two phases** (07-07-2026 — 3 production runs died at "Reached maximum
  number of turns (30)" because ONE budget had to cover diagnose+patch+verify):
  a read-only diagnosis run (`SELF_IMPROVEMENT_DIAGNOSIS_MAX_TURNS`, 15) ends
  with a `DIAGNOSIS_JSON` verdict (noop / email_user / fix, plus
  `record_known_gate` for external gates); only a `fix` verdict starts the
  patch run, which gets the full `SELF_IMPROVEMENT_MAX_TURNS` budget to
  itself and the diagnosis injected. Diagnoses the failure from the redacted
  transcript/logs/code (plus `incident.prior_attempts` — see
  `incident_store`), then either
  does nothing, emails the user, or patches + verifies (`just check` in the
  worktree, which finds an already-installed environment via a `.venv`
  *symlink* to `PROJECT_ROOT`'s real one — `uv run` follows it transparently
  since the checked-out `pyproject.toml`/`uv.lock` are byte-identical, so
  there's no per-run `uv sync`. Setting `VIRTUAL_ENV` alone does **not**
  achieve this — `uv` only prefers an external venv via the `--active` CLI
  flag, which has no env-var equivalent and isn't in the justfile's `uv run`
  calls; that was tried, empirically failed, and was replaced with the
  symlink) + commits + pushes via a dedicated `commit_push_deploy` tool (never
  raw `git` — a `can_use_tool` callback denies `git commit`/`git push`/`git
  reset` through `Bash`). If `SELF_IMPROVEMENT_ALLOW_DEPLOY=1` and a
  fast-forward is still possible, it pushes straight to `origin main` —
  **that push is the deploy trigger**, picked up by the existing `ci.yml` ->
  `deploy.yml` pipeline; there is no separate local deploy script anymore.
  Otherwise (deploy disabled, or `main` moved during the run) it pushes a
  `self-improvement/<ts>` review branch instead and emails the user to merge
  by hand. **When every push fails** (verified in production: the VPS deploy
  key was read-only, so five correct fixes in a row were written and lost),
  the commit is saved as a `git am`-able patch in `state/pending_patches/`
  and attached to the alert email — a verified fix must never die with the
  worktree. Browser diagnostics (`browser_open`, `browser_diagnostics`,
  `browser_safe_click`, `browser_screenshot`) are custom MCP tools over the
  same shared CDP browser, guarded by `browser_lock`. Routed through a local
  **LiteLLM proxy** (`deploy/litellm.config.yaml`, `just litellm-proxy`)
  that presents an Anthropic-Messages-API-shaped endpoint backed by
  `deepseek/deepseek-v4-pro` — real Anthropic credit is not spent. Model/
  turns/budget/timeout via `SELF_IMPROVEMENT_PROXY_MODEL` (default
  `self-improvement-deepseek`), `SELF_IMPROVEMENT_MAX_TURNS`,
  `SELF_IMPROVEMENT_MAX_BUDGET_USD`, `SELF_IMPROVEMENT_TIMEOUT_SECONDS`.
- `src/notify.py` — emails `NOTIFY_TO` after each handled listing
  (outcome + redacted summary) via Gmail `send` scope. Also the single
  integration point for web push: `send_status_email` calls
  `push_notify.push_status` first (own flag/filter, never raises).
- `src/push_notify.py` — **native notifications (Chrome desktop + Android)**
  via the standard Web Push API + VAPID; no third-party account. VAPID keys
  auto-generate into `state/vapid.json`; per-device subscriptions live in
  `state/push_subscriptions.jsonl` (expired endpoints pruned on send). The
  dashboard serves the plumbing (`/sw.js` from root scope, `/push/public-key`,
  `/push/subscribe`, `/push/unsubscribe`, `/push/test`) and a 🔔 toggle in the
  nav; the actual send happens in whichever process records the outcome
  (orchestrator/poller). Enable per device: open the dashboard, click 🔔 once.
  Env: `WEB_PUSH_ENABLED=0` to disable, `WEB_PUSH_OUTCOMES` (default
  `submitted`) to widen.
- `src/healthcheck.py` (+ systemd timer, 30 min) — alerts (push + email) when:
  DeepSeek credit is low; a pipeline systemd unit is down (orchestrator/poller/
  browser-host/litellm-proxy — a crash-looping orchestrator is otherwise
  invisible, see hard-won lessons); a session expired on Stekkies or a top
  apply site (`SITE_PROBES`: huurwoningen.nl, kamernet.nl — extend via
  `HEALTHCHECK_SITE_PROBES` JSON); or the **last
  `SELF_IMPROVEMENT_HEALTH_WINDOW` (5) self-improvement runs all failed**
  (27 identical crashes on 01-07-2026 went unnoticed as a pattern — the
  layer that repairs failures needs its own watcher). Also sends the weekly
  `src/digest.py` summary, piggybacked on this timer. Site probes run in the real shared browser
  under `browser_lock(timeout=60)` and are skipped when an apply is in flight.
  Optionally GETs `HEALTHCHECK_PING_URL` (dead-man's switch, e.g.
  healthchecks.io) at the end of each run — its *absence* alerts on total-box
  death. `remaining_credit()` shared here.
- `src/listing_context.py` — one cheap httpx GET of a listing's own detail
  page (JSON-LD): description/price/surface. Used by `poller.watcher._enrich`
  (anchor-parser sites yield URL-only listings, so the filter/judge were
  blind on them) and by `apply.build_prompt` (description + aggregator
  warning in the prompt up front). Strictly fail-open; tier-3 pages just
  fail the fetch and nothing changes.
- `src/dashboard/` — FastAPI + htmx/Chart.js read-mostly dashboard behind Caddy
  (HTTPS + Basic Auth), organized around four decision questions. **Overview**
  (`/`): an action-needed strip (`healthinfo.attention_items` — service down,
  low credit, logged-out session, unlanded pending patches, active paid gates,
  self-improvement failing streak, stuck browser lock, blocked poller sites)
  plus mission KPIs (submissions, success rate, detection→submitted latency,
  race wins, weekly spend + cost/submission). **Funnel** (`/funnel`,
  `src/dashboard/funnel.py`): per-source seen→filtered→judged→qualified→
  attempted→submitted (leak rows flagged), failure + incident Paretos, filter/
  judge veto-reason breakdown, and the mail race (moved here). **Self-
  improvement** (`/self-improvement`, `src/dashboard/si.py`): SI runs with
  per-run cost, incidents, editable known-gates table (delete a wrongly-gated
  site via `known_gates.remove_gate`), pending patches (read-only, copyable
  `git am`), guard-fire trend, playbooks. **Forensics** (`/submission/{key}`):
  a per-turn trajectory timeline (`src/dashboard/trajectories.py`, from
  `logs/trajectories/*.jsonl` with a transcript-regex fallback) + token-per-turn
  chart + collapsed redacted transcript. Data layer: `src/dashboard/cache.py`
  (`JsonlTail` incremental append-only parse + `memo` TTL cache — the overview
  used to trigger 5+ full re-reads/request), `costs.py` (trajectory-first per-run
  cost + weekly rollups, rates from `src/llm_pricing.py` shared with the
  self-improvement agent). Stable content-hash permalinks (`Submission.permalink`;
  legacy `/submission/<int>` still resolves). Never serves `*.prompt.txt`;
  everything user-visible goes through `data.redact()`. Static assets in
  `static/` (theme-aware light/dark). Safe POST actions return an htmx toast;
  poller pause/resume needs the `deploy/stekkies-dashboard.sudoers` entries
  (re-synced every deploy by `ensure-self-improvement.sh`).
- `justfile` — every workflow as a `just` command (local + VPS ops + secret push).

## Conventions
- Python 3.12, managed by **uv** (`pyproject.toml` + `uv.lock`). `uv sync` to
  install; prefix commands with `uv run` (no manual venv activate).
- Run modules as packages: `uv run python -m src.<module>`.
- **`just check` runs the unit tests** (`unittest discover tests`), not just
  lint + import smoke. This is deliberate: it is also the self-improvement
  agent's verify gate (`SELF_IMPROVEMENT_VERIFY_CMD`), and an autonomous patch
  that pushes straight to main must not pass on lint alone (found the hard
  way: the suite sat broken for a while because nothing ran it).
- **Use the `justfile` recipes for common workflows** instead of reinventing
  commands — run `just` (or read the `justfile`) to list them. Covers local dev
  (`sync`, `host`, `login`, `watch`, `dashboard`, `healthcheck`, `reauth`,
  `ensure-claude-cli`, `litellm-proxy`), VPS ops (`deploy`, `pull`, `shell`,
  `logs`, `status`, `pause`/`resume`, `credits`, `vnc`), and secret push
  (`push-creds`, `push-token`, `push-env`). `deploy` (and CI's `deploy.yml`)
  both call `deploy/ensure-self-improvement.sh` on every deploy — an
  already-provisioned VPS self-heals to whatever `claude`
  CLI/`litellm-proxy.service` state the repo now expects, not just a fresh
  `deploy/setup.sh` install.
- Local dev: WSL2 + WSLg (DISPLAY=:0) for headed Chromium. VPS: Xvfb (DISPLAY
  =:99). No system Chrome — use bundled Chromium. Docs live in `documents/`.
- `state/` (profile, creds, tokens) and `logs/` are gitignored — never commit.
- The agent applies and **submits** autonomously — there is no dry-run guard.
- Secrets: `state/sources_credentials.json` (plaintext, local-only). Never print
  passwords in logs or commits.

## Gotchas
- Google blocks automation browsers; host launches with
  `--disable-blink-features=AutomationControlled` + no `--enable-automation`.
- The agent needs `DEEPSEEK_API_KEY` (env; on the VPS via `state/agent.env`,
  loaded by the orchestrator systemd unit). Watch for HTTP 402 (credits).
- **VPS runtime config = `state/agent.env`**, not `.env`. Both `orchestrator`
  and `dashboard` systemd units read it via `EnvironmentFile=`. Besides
  `DEEPSEEK_API_KEY` it carries `GOOGLE_ACCOUNT`, `NOTIFY_TO`, and the
  `APPLICANT_*` profile vars. systemd parses `KEY=VALUE` with spaces/parens
  fine (e.g. an `APPLICANT_EMPLOYMENT` with commas), but bash `source` chokes on
  those — verify what a service actually sees via
  `/proc/$(systemctl show -p MainPID --value orchestrator)/environ`, not by
  sourcing the file.
- **Back up `documents/` before any `git reset --hard`/pull on the VPS.** The
  PDFs are gitignored now, but an older checkout may still have them *tracked*;
  a hard reset to a tree where they're absent deletes them. Always
  `tar czf <backup> documents state` first, reset, then `tar xzf <backup>
  documents`. Run git as the deploy user (the repo is deploy-owned).
- Node/npx is required at runtime for the Playwright MCP.
- The self-improvement agent needs the **`claude` CLI on PATH**
  (`npm install -g @anthropic-ai/claude-code` — `claude-agent-sdk` shells out
  to it) and the **LiteLLM proxy running** (`litellm-proxy.service` on the
  VPS, `just litellm-proxy` locally) — it points `ANTHROPIC_BASE_URL` at the
  proxy (`127.0.0.1:4000`) instead of api.anthropic.com, so no real
  `ANTHROPIC_API_KEY` is needed; `ANTHROPIC_AUTH_TOKEN` is a placeholder that
  only satisfies the CLI's own "am I configured" check. The proxy reuses the
  same `DEEPSEEK_API_KEY` already in `state/agent.env`.
- **DeepSeek via LiteLLM silently mishandles two Claude-specific request
  params — do not send them on this path.** `thinking`/`effort` wrap
  DeepSeek's entire reply in a fake `thinking` block that rambles until it
  hits `max_tokens` with zero real output (same "reasoning truncation =
  silent stall" failure class as the hard-won lesson below, via a new path).
  `output_config.format` (structured output) is silently ignored — no
  error, just a free-text reply instead of schema-JSON — so
  `self_improvement_agent.py` extracts the final result from a text marker,
  not `ResultMessage.structured_output`. Verified directly against the
  proxy with curl, not assumed.
- **`ResultMessage.total_cost_usd` / `model_usage[...].costUSD` are wrong for
  this proxied model — off by ~19.5x, verified, not estimated.** Claude
  Code's own client-side cost calculator doesn't recognize a custom
  `model_name` like `self-improvement-deepseek` and falls back to some
  default rate. A run whose real cost (computed from the logged raw
  `usage` tokens × deepseek-v4-pro's actual published per-token rates) was
  $0.030 was reported by the SDK as $0.586. Trust
  `_estimate_deepseek_cost_usd()`'s logged `estimated_cost_usd`, not the
  SDK's own cost field — and note `max_budget_usd` is checked against the
  SDK's *inflated* number, so it's set ~20x higher than the real dollar
  ceiling you actually want (see the constant's comment).
- Stekkies only notifies; the real application is on the external source site,
  which varies per listing — hence the LLM agent for the last mile.
- Transcripts/prompts can contain plaintext site passwords — the dashboard
  redacts them and never serves `*.prompt.txt`. Don't undo that.

## Hard-won lessons (don't relearn these)
- **Alerting must not share a failure mode with what it monitors.** The Gmail
  refresh token was revoked on 04-07-2026; the orchestrator crash-looped 1136
  times over 3+ days and NO alert could reach the user because alert email
  used that same dead token. Fixes: `notify.send_alert` pushes (web push)
  BEFORE emailing; the orchestrator's watch loop catches Gmail failures
  in-process (alert + `WATCH_RETRY_SECONDS` backoff, no systemd crash loop);
  the healthcheck checks unit liveness and supports a dead-man ping. NB: a
  Google OAuth app in *Testing* status expires refresh tokens every 7 days —
  publish it to Production or this recurs weekly.
- **asyncio's default executor is tiny and DNS shares it.** `asyncio.to_thread`
  AND `loop.getaddrinfo` both use the loop's default executor (8 threads on a
  4-vCPU box). ~13 tier-3 watchers parking threads on the browser flock
  starved DNS, so every pending httpx connect timed out AT ONCE — 10k+
  ConnectTimeout poll_errors/day, ~80% of tier-2 polls silently lost
  (diagnosed 07-07-2026 from all-sites-simultaneous timeout bursts). Fixes:
  a 64-thread default executor (`POLL_EXECUTOR_THREADS`), tier-3 polls give
  up on the lock after `POLL_TIER3_LOCK_TIMEOUT` (120s) instead of queueing
  30 min, and startup polls are staggered.
- **`asyncio.wait_for` cannot unwedge a hung MCP teardown.** It only cancels
  the task; the cancellation still unwinds `stdio_client.__aexit__`, which
  waits on the npx process — if that ignores closed stdin, `asyncio.run()`
  blocks forever holding the browser flock (03-07-2026: 9+ hours, eight
  consecutive mail applies starved out at 1800s each). `run_agent` now arms a
  `threading.Timer` watchdog that SIGKILLs wedged MCP descendants
  `APPLY_TEARDOWN_GRACE_SECONDS` (120s) past the wall-clock timeout, and
  `browser_lock` records its holder + pushes an alert after a 300s wait.
- **HTTP 402 (out of credit) is not a verdict on the listing.** Every apply
  outcome used to consume the listing (one-attempt rule) — so during a credit
  outage every listing that dropped was burned forever as "error". outcome
  `no_credit` (rc=126) is now a third carve-out alongside `yielded` and the
  browser-lock timeout: poller releases the claim, orchestrator leaves the
  mail unread, both alert (rate-limited via `notify.send_alert_dedup`).
- **Model:** `deepseek-v4-pro` is the default apply model. Keep
  `gemini-3.5-flash` avoided; it falls into degenerate loops (e.g. ArrowDown ×30).
- **Reasoning truncation = silent stall.** Reasoning models can emit hidden
  reasoning tokens (counted against the completion budget) before any
  content/tool_call. Over a big page snapshot that reasoning can exhaust the
  completion cap mid-thought, so the API returns `finish_reason="length"` with
  empty content AND no tool_calls. The loop reads that as "the model stopped",
  burns its 2 nudges, and bails after a few seconds with no real attempt. This
  sank kamernet submission #25 (29-06-2026). Fixes in `browser_agent.py`:
  (1) thinking is **disabled by default** via
  `extra_body={"thinking":{"type":"disabled"}}` — form-filling needs no heavy reasoning. Re-enable with
  `APPLY_REASONING_EFFORT`.
  (2) explicit `max_tokens` headroom; (3) truncated-empty turns (`finish_reason=
  length`) are retried, not counted as a conclusion; (4) per-turn log of
  `finish_reason` + `completion/reasoning_tokens`. NB: the reasoning *text* stays
  hidden — only the token *count* is exposed (`usage.completion_tokens_details`).
  Also: the transcript's tool-arg log no longer clamps urls/refs to 60 chars (that
  clamp made a full URL look truncated and masked the real cause).
- **Use the Playwright MCP high-level tools** (snapshot→ref→click/fill_form);
  the raw-JS path (`browser_cdp`/`browser_evaluate`) caused 50+ calls + full-page
  dumps for one task. They stay filtered out.
- **Accessibility-tree snapshots miss dialogs built without proper ARIA
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
- **Already-applied = STOP, never resubmit.** Detect by control wording
  ("Aanvraag wijzigen", "Reactie intrekken", "je hebt gereageerd", "Doorgaan
  met gesprek"). Pre-filled fields / saved docs alone do NOT mean already-applied.
- **Hof van Oslo, resolved (02-07-2026):** the above dialog-blindspot fix
  (`dom_scan`/`click_by_text`) alone wasn't enough — three more real, verified
  bugs surfaced only once the actual REBO Groep dialog was driven end to end.
  (1) `dom_scan`'s "current page" picked the last-*created* tab, not the one
  the Playwright MCP actually had selected — with several tabs open (SSO
  popups, an inschrijfportaal tab...) that's silently the wrong tab. Fixed by
  asking the MCP's own `browser_tabs` listing (which marks the true current
  tab with `(current)`) and passing that URL through as a hint (`current_page`
  in `browser_dom_tools.py`). (2) an uncaught `Locator.click` timeout inside
  `click_by_text` propagated out and killed the whole MCP session/process —
  now caught and returned as a normal (recoverable) tool result. (3) there was
  no way to *type* into a ref-less dialog's inputs at all — `dom_scan` can
  read, `click_by_text` can only click. Added `fill_by_label` and
  `select_option_by_label` (see architecture section above) — the missing
  piece that actually let the agent complete the form. Also found: REBO
  Groep's page has a button labelled "Inschrijven huuraanbod" that opens a
  **paid €34,95/year email-alert subscription**, not an application for the
  listing — a real dark pattern, verified by inspecting the dialog's DOM
  directly (title: "Schrijf je in voor onze e-mailservice"), not assumed;
  `apply.py`'s prompt now warns against it by name. With all of the above,
  the same listing went from a 60-turn timeout to a 23-turn real submission.
- **Duplicate HTML ids break scoped lookups, not just accessibility.**
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
- **Custom dropdown options can default to `type="submit"`.** REBO Groep's
  "Soort inkomen" options are `<button>`s with no explicit `type="button"`
  inside a `<form>` — clicking one to just *select* it can fire a real,
  premature form submission before the rest of the dialog is filled (verified:
  selecting the option early showed the browser's native "Vul dit veld in"
  validation on every other still-empty required field). `select_option_by_label`
  now attaches a one-time capturing submit-preventing guard to every form right
  before clicking an option.
- **One site, one listing, several URL shapes — path-based keying can't
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
- **Cross-source dedup gap: the same listing, two different keys.** The
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
- **Stale page dumps in history = quadratic input tokens.** Every
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
- **Source sites gate you:** ikwilhuren Plus paywall (2-day delay for standard
  accounts), MijnDak needs a per-region inschrijving + eligibility recompute.
  These are real states to report, not bugs — stop early and label them.
- **Hard published eligibility gates are readable at poll time.** Full agent
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
- **Gmail listing mails:** from `help@stekkies.com`; the listing link is a
  hex-hash `http://www.stekkies.com/.../redirect/<hash>` and the body is
  quoted-printable (must QP-decode before regex).
- **Documents** are uploaded in a fixed priority order with a one-line purpose
  each (see `_classify` in `apply.py`): ID → werkgeversverklaring → recent
  payslips → landlord ref → profile → motivatiebrief → UWV → jaaropgave →
  bank → degiro. Keep
  the expired arbeidsovereenkomst OUT; keep the bank statement trimmed.
