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
- `documents/` — application PDFs/JPG, version-controlled so the VPS gets them
  via git. `DOCS_DIR` (config) points here; override with the env var.
- `src/credentials.py` / `import_passwords.py` — per-site logins by domain.
- `src/gmail_watch.py` — poll inbox (5s), extract Stekkies link.
- `src/orchestrator.py` — ties it together (`--once URL` or live watch); logs the
  true `outcome` (submitted / already_applied / not_available / …) and only marks
  a listing processed when the outcome is terminal.
- `src/notify.py` — emails you@example.com after each handled listing
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
- Node/npx is required at runtime for the Playwright MCP.
- Stekkies only notifies; the real application is on the external source site,
  which varies per listing — hence the LLM agent for the last mile.
- Transcripts/prompts can contain plaintext site passwords — the dashboard
  redacts them and never serves `*.prompt.txt`. Don't undo that.

## Hard-won lessons (don't relearn these)
- **Model:** `z-ai/glm-5.2` works well *in our own loop*. Its earlier
  empty/reasoning-only stalls were a **Hermes**-loop artifact, not the model.
  `gemini-3.5-flash` falls into degenerate loops (e.g. ArrowDown ×30) — avoid.
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
