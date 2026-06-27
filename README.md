# Stekkies auto-responder

Fully-autonomous responder for Stekkies rental matches.

**Pipeline:** Gmail (new Stekkies mail) → open Stekkies listing with your saved
login, extract listing metadata + external "Go to listing" URL → hand off to
our **browser agent** (`src/browser_agent.py`), which opens the source site
(Ik Wil Huren, Pararius, Funda, …), writes a customized message from the
reference template, fills the application form, uploads your documents, and
submits.

Stekkies is only a *notifier/aggregator* — the real application happens on the
external source site, which varies per listing. That variable last mile is why
the apply stage uses an LLM browser agent rather than a fixed script.

## One shared logged-in browser (CDP)
A single persistent Chromium — the **browser host** (`src.browser_host`) — runs
with a CDP debugging port. Both the Stekkies extractor and the apply agent
**attach to it over CDP**, so every session lives in one profile you sign into
once: Google (enables "Sign in with Google" SSO on Funda etc.), Stekkies, and all
rental sites. The apply agent drives it via the Playwright MCP (`--cdp-endpoint`),
and needs `OPENROUTER_API_KEY` set.

## Layout
- `src/config.py`       — paths, URLs, `CDP_URL`.
- `src/browser_host.py` — always-on shared Chromium (CDP port); `--login` opens sites to sign into.
- `src/login_setup.py`  — (legacy) standalone Stekkies login; the browser host replaces it.
- `src/stekkies.py`     — attach over CDP, open a listing, extract metadata + source URL.
- `src/credentials.py`  — per-site logins matched by domain (from import_passwords).
- `src/import_passwords.py` — load a Google Password Manager CSV into the creds JSON.
- `src/message_template.py` — reference application message the agent customizes.
- `src/apply.py`        — build the task prompt (SSO-first, creds, docs), run the agent.
- `src/browser_agent.py` — the agent loop (OpenRouter + Playwright MCP); returns a structured outcome.
- `src/gmail_watch.py`  — detect new Stekkies mails, extract the listing link.
- `src/orchestrator.py` — ties it all together.
- `state/`              — Chromium profile, creds, Gmail token, agent.env (do not commit).
- `logs/`               — `activity.log`, `mail_summary.jsonl`, `transcripts/`.

## Setup
Uses [uv](https://docs.astral.sh/uv/). `uv sync` creates `.venv` from
`pyproject.toml` + `uv.lock`. Prefix commands with `uv run` (no manual activate).
```bash
uv sync
uv run playwright install chromium

# 1. Start the shared browser host and log into everything ONCE:
uv run python -m src.browser_host --login   # opens Google, Stekkies, rental sites
#   -> in that window: sign into Google first (for SSO), then Stekkies and the
#      rental sites. Leave this process running (own terminal / service).

# 2. Import rental-site passwords (optional, for non-SSO sites):
uv run python -m src.import_passwords passwords.csv   # then delete the CSV

# 3. Gmail access: create a Desktop OAuth client in Google Cloud project
#    your-gcp-project, download JSON to state/gmail_client_secret.json
#    First run authorizes in the browser and caches state/gmail_token.json.
```

## Run
```bash
# Process a single Stekkies listing (applies + submits autonomously):
uv run python -m src.orchestrator --once "https://www.stekkies.com/en/api/v1/h/redirect/5338905"

# Live: watch inbox and auto-respond:
uv run python -m src.orchestrator
```

The agent applies and **submits** autonomously — there is no dry-run guard.

## Monitoring
For a concise operational view over SSH:
```bash
tail -f logs/activity.log
tail -f logs/mail_summary.jsonl
```
`activity.log` is human-readable: one short line per handled email/listing.
`mail_summary.jsonl` is structured JSONL with message id, status, listing URLs,
address/source when known, return code, duration, and the short outcome message.

## Dashboard
`src/dashboard/` is a FastAPI app (run `uv run uvicorn src.dashboard.app:app`)
showing stats/charts, submissions, per-listing **redacted** transcripts, a live
health panel (services + OpenRouter credit + Stekkies login), and safe actions
(retry / pause / resume / run health check). On the VPS it runs as
`dashboard.service` on `127.0.0.1:8000` behind **Caddy** (auto-HTTPS + Basic
Auth) at the DuckDNS domain. Transcripts are scrubbed of credentials and
`*.prompt.txt` is never served.

## Deploy (24/7)
See [`deploy/README.md`](deploy/README.md) — Hetzner VM + Xvfb + systemd + VNC +
Caddy dashboard.

## Open items / required input
- **Latency.** Polling is every 5s (`src/gmail_watch.py`). For lower latency,
  use Gmail push (Pub/Sub `users().watch()` + a webhook endpoint / tunnel).
- **Gmail query.** Tune `GMAIL_QUERY` in `src/gmail_watch.py` to your real
  Stekkies sender/subject.
