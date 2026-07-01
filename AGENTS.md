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
  each snapshot follows a *different* click), and returns a structured
  `AgentResult(rc, outcome, summary)`. Two local (non-MCP) fallback tools,
  `dom_scan`/`click_by_text` (`src/browser_dom_tools.py`, shared with
  self-improvement's own browser diagnostics), give the model a way past
  HTML dialogs/overlays that lack proper ARIA roles and never get a
  `browser_snapshot` ref — raw DOM query + click-by-visible-text instead of
  the accessibility tree. Not arbitrary JS: each is one fixed, narrow
  operation, unlike the still-blocked `browser_evaluate`.
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
  `processed_listings.jsonl`, `browser_lock.py` is a cross-process flock so the
  poller's applier and the Stekkies orchestrator never drive the shared browser
  at once (also wired into `apply.apply()`). `registry.py` lists all 26 sites;
  `discover.py` probes which tier each site currently yields. Run: `just poll`,
  `just poll-once <site>`, `just discover`. A non-terminal outcome
  (incomplete/timeout/error/unknown) releases the listing so the next poll
  retries it — but a listing that fails the same non-terminal way every time
  (e.g. a page that reliably burns the whole turn budget) is capped at
  `MAX_POLLER_ATTEMPTS` (default 2, env `POLLER_MAX_ATTEMPTS`) via
  `dedup.release_count()`; past the cap it's marked seen/processed like a
  terminal outcome so it stops being retried. Without this a stuck listing
  gets re-applied every cadence forever, burning real LLM cost for a result
  that will never change (seen: `Hof van Oslo` retried 15+ times over several
  hours on 2026-07-01, several million tokens each).
- `src/self_improvement_agent.py` — runs after a non-terminal apply outcome
  (blocked/error/incomplete/timeout/not_available/…, see
  `SELF_IMPROVEMENT_OUTCOMES`). Drives the **Claude Agent SDK**
  (`claude_agent_sdk.query()` — the same engine behind Claude Code: real
  `Read`/`Edit`/`Bash`/`Grep`/`Glob`). Each run creates a **throwaway git
  worktree** (`_create_worktree`, a sibling dir
  `../browser-agent-self-improvement-worktrees/<ts>` branched off a
  freshly-fetched `origin/main` — never `PROJECT_ROOT` itself, so a run can
  never collide with the live checkout or in-progress human edits) and
  always removes it in a `finally` (`_remove_worktree`), even on timeout.
  Diagnoses the failure from the redacted transcript/logs/code, then either
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
  by hand. Browser diagnostics (`browser_open`, `browser_diagnostics`,
  `browser_safe_click`, `browser_screenshot`) are custom MCP tools over the
  same shared CDP browser, guarded by `browser_lock`. Routed through a local
  **LiteLLM proxy** (`deploy/litellm.config.yaml`, `just litellm-proxy`)
  that presents an Anthropic-Messages-API-shaped endpoint backed by
  `deepseek/deepseek-v4-pro` — real Anthropic credit is not spent. Model/
  turns/budget/timeout via `SELF_IMPROVEMENT_PROXY_MODEL` (default
  `self-improvement-deepseek`), `SELF_IMPROVEMENT_MAX_TURNS`,
  `SELF_IMPROVEMENT_MAX_BUDGET_USD`, `SELF_IMPROVEMENT_TIMEOUT_SECONDS`.
- `src/notify.py` — emails `NOTIFY_TO` after each handled listing
  (outcome + redacted summary) via Gmail `send` scope.
- `src/healthcheck.py` (+ systemd timer, 30 min) — emails when DeepSeek credit
  is low or the Stekkies session has expired. `remaining_credit()` shared here.
- `src/dashboard/` — FastAPI + htmx/Chart.js read-only dashboard (stats, per-run
  redacted transcripts, health, safe actions) behind Caddy (HTTPS + Basic Auth).
- `justfile` — every workflow as a `just` command (local + VPS ops + secret push).

## Conventions
- Python 3.12, managed by **uv** (`pyproject.toml` + `uv.lock`). `uv sync` to
  install; prefix commands with `uv run` (no manual venv activate).
- Run modules as packages: `uv run python -m src.<module>`.
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
- **Source sites gate you:** ikwilhuren Plus paywall (2-day delay for standard
  accounts), MijnDak needs a per-region inschrijving + eligibility recompute.
  These are real states to report, not bugs — stop early and label them.
- **Gmail listing mails:** from `help@stekkies.com`; the listing link is a
  hex-hash `http://www.stekkies.com/.../redirect/<hash>` and the body is
  quoted-printable (must QP-decode before regex).
- **Documents** are uploaded in a fixed priority order with a one-line purpose
  each (see `_classify` in `apply.py`): ID → werkgeversverklaring → recent
  payslips → landlord ref → profile → motivatiebrief → UWV → jaaropgave →
  bank → degiro. Keep
  the expired arbeidsovereenkomst OUT; keep the bank statement trimmed.
