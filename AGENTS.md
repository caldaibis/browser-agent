# AGENTS.md

Stekkies rental auto-responder. Pipeline: Gmail (new Stekkies mail) → extract
response letter + external source URL → Hermes browser agent applies on the
source site (logs in, fills form, uploads docs, submits).

## Architecture
- **One shared browser** (`src/browser_host.py`): persistent Chromium on CDP
  port 9222. The Stekkies extractor and Hermes both attach over CDP, so all
  logins (Google SSO + rental sites) live in one profile signed into once.
- `src/stekkies.py` — attach over CDP, extract letter/source URL (deterministic).
- `src/apply_hermes.py` — build prompt, run `hermes chat -t playwright` via pty
  (live output). Uses the **Playwright MCP** (registered with `hermes mcp add`,
  `--cdp-endpoint http://127.0.0.1:9222`) for efficient snapshot/click/fill_form
  + `browser_file_upload`. Do NOT re-enable Hermes's built-in `browser` toolset:
  its low-level `browser_cdp` caused 50+ raw-JS calls + full-page dumps.
  The playwright MCP's `browser_evaluate` + `browser_run_code_unsafe` are
  disabled (`hermes tools disable playwright:browser_evaluate
  playwright:browser_run_code_unsafe`) to force efficient high-level tool use;
  this lives in ~/.hermes/config.yaml (copied to the VPS).
- Apply model: google/gemini-3.5-flash (HERMES_MODEL). GLM-5.2 stalls with
  empty/reasoning-only responses in Hermes; avoid.
- `documents/` — application PDFs/JPG, version-controlled so the VPS gets them
  via git. `DOCS_DIR` (config) points here; override with the env var.
- `src/credentials.py` / `import_passwords.py` — per-site logins by domain.
- `src/gmail_watch.py` — poll inbox, extract Stekkies link.
- `src/orchestrator.py` — ties it together (`--once URL` or live watch).

## Conventions
- Python 3.12, managed by **uv** (`pyproject.toml` + `uv.lock`). `uv sync` to
  install; prefix commands with `uv run` (no manual venv activate).
- Run modules as packages: `uv run python -m src.<module>`.
- WSL2 + WSLg (DISPLAY=:0) for headed Chromium; no system Chrome (use bundled
  Chromium). Docs live on a Windows path under `/mnt/c/...`.
- `state/` (profile, creds, tokens) and `logs/` are gitignored — never commit.
- **`DRY_RUN`** in `src/config.py` gates real submits; keep `True` until verified.
- Secrets: `state/sources_credentials.json` (plaintext, local-only). Never print
  passwords in logs or commits.

## Gotchas
- Google blocks automation browsers; host launches with
  `--disable-blink-features=AutomationControlled` + no `--enable-automation`.
- Hermes uses OpenRouter by default — watch for HTTP 402 (credits); direct
  OpenAI/Google keys are configured as alternatives.
- Stekkies only notifies; the real application is on the external source site,
  which varies per listing — hence the LLM agent for the last mile.
