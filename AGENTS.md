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
  AsyncOpenAI (OpenRouter) tool-calling over the **Playwright MCP**
  (`npx @playwright/mcp@latest --cdp-endpoint http://127.0.0.1:9222`) for
  snapshot/click/fill_form/file_upload. Filters raw-JS tools (browser_evaluate,
  browser_run_code_unsafe), has a repeat-action guard + capped nudges, and
  returns a structured `AgentResult(rc, outcome, summary)`.
- `src/apply.py` — build the prompt, run the agent, persist a per-run transcript
  to `logs/transcripts/<ts>_<source>_<address>.log`. Apply model: `z-ai/glm-5.2`
  (override via `APPLY_MODEL`); gemini-3.5-flash was too flaky.
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
- `src/notify.py` — emails `NOTIFY_TO` after each handled listing
  (outcome + redacted summary) via Gmail `send` scope.
- `src/healthcheck.py` (+ systemd timer, 30 min) — emails when OpenRouter credit
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
  (`sync`, `host`, `login`, `watch`, `dashboard`, `healthcheck`, `reauth`), VPS
  ops (`deploy`, `pull`, `shell`, `logs`, `status`, `pause`/`resume`, `credits`,
  `vnc`), and secret push (`push-creds`, `push-token`, `push-env`).
- Local dev: WSL2 + WSLg (DISPLAY=:0) for headed Chromium. VPS: Xvfb (DISPLAY
  =:99). No system Chrome — use bundled Chromium. Docs live in `documents/`.
- `state/` (profile, creds, tokens) and `logs/` are gitignored — never commit.
- The agent applies and **submits** autonomously — there is no dry-run guard.
- Secrets: `state/sources_credentials.json` (plaintext, local-only). Never print
  passwords in logs or commits.

## Gotchas
- Google blocks automation browsers; host launches with
  `--disable-blink-features=AutomationControlled` + no `--enable-automation`.
- The agent needs `OPENROUTER_API_KEY` (env; on the VPS via `state/agent.env`,
  loaded by the orchestrator systemd unit). Watch for HTTP 402 (credits).
- **VPS runtime config = `state/agent.env`**, not `.env`. Both `orchestrator`
  and `dashboard` systemd units read it via `EnvironmentFile=`. Besides
  `OPENROUTER_API_KEY` it carries `GOOGLE_ACCOUNT`, `NOTIFY_TO`, and the
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
- Stekkies only notifies; the real application is on the external source site,
  which varies per listing — hence the LLM agent for the last mile.
- Transcripts/prompts can contain plaintext site passwords — the dashboard
  redacts them and never serves `*.prompt.txt`. Don't undo that.

## Hard-won lessons (don't relearn these)
- **Model:** `z-ai/glm-5.2` works well *in our own loop*. Its earlier
  empty/reasoning-only stalls were a **Hermes**-loop artifact, not the model.
  `gemini-3.5-flash` falls into degenerate loops (e.g. ArrowDown ×30) — avoid.
- **Reasoning truncation = silent stall.** glm-5.2 is a reasoning model and emits
  hidden reasoning tokens (counted against the completion budget) before any
  content/tool_call. Over a big page snapshot that reasoning can exhaust the
  completion cap mid-thought, so the API returns `finish_reason="length"` with
  empty content AND no tool_calls. The loop reads that as "the model stopped",
  burns its 2 nudges, and bails after a few seconds with no real attempt. This
  sank kamernet submission #25 (29-06-2026). Fixes in `browser_agent.py`:
  (1) reasoning is **disabled by default** via `extra_body={"reasoning":{"enabled":
  False}}` — form-filling needs no heavy reasoning, and glm IGNORES fine-grained
  reasoning caps (`max_tokens`/`effort` barely move it) but honours `enabled:False`
  → 0 reasoning tokens, still correct. Re-enable with `APPLY_REASONING_EFFORT`.
  (2) explicit `max_tokens` headroom; (3) truncated-empty turns (`finish_reason=
  length`) are retried, not counted as a conclusion; (4) per-turn log of
  `finish_reason` + `completion/reasoning_tokens`. NB: the reasoning *text* stays
  hidden — only the token *count* is exposed (`usage.completion_tokens_details`).
  Also: the transcript's tool-arg log no longer clamps urls/refs to 60 chars (that
  clamp made a full URL look truncated and masked the real cause).
- **Use the Playwright MCP high-level tools** (snapshot→ref→click/fill_form);
  the raw-JS path (`browser_cdp`/`browser_evaluate`) caused 50+ calls + full-page
  dumps for one task. They stay filtered out.
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
  payslips → landlord ref → profile → UWV → jaaropgave → bank → degiro. Keep
  the expired arbeidsovereenkomst OUT; keep the bank statement trimmed.
