# Stekkies auto-responder

An autonomous responder for rental alerts: Gmail → Stekkies or Huurwoningen →
source site → submitted application.

Stekkies is usually only the notifier. The real application happens on a
landlord, agency, or rental-platform site, so the last mile uses a DeepSeek
browser agent over a pinned `agent-browser` MCP interface. It fills forms,
customizes the reference message, uploads selected documents, and submits.

> **Safety:** there is no dry-run confirmation. A live single-listing or watcher
> command can submit a real application and upload personal documents.

## How it fits together

One persistent Chromium process keeps Google, Stekkies, and rental-site sessions
in a shared profile. Deterministic extraction and health/session probes attach
to CDP directly; the apply loop attaches through the normalized agent-browser
adapter. Cross-process access is serialized by a browser lock.

```text
Gmail -> orchestrator -> extract/deduplicate/policy -> apply agent -> outcome
                                                        |
                                                        +-> shared Chromium
                                                        +-> documents
outcome -> SQLite + redacted evidence + notification + optional repair queue
```

For the full process, data, and module map, see
[`docs/architecture.md`](docs/architecture.md).

## Repository map

| Path | Purpose |
|---|---|
| `src/orchestrator.py` | Listing lifecycle, dedup, persistence, mail, notifications |
| `src/stekkies.py`, `src/gmail_watch.py` | Deterministic intake and extraction |
| `src/apply.py` | Apply-stage pre-flight and agent facade |
| `src/browser_agent/` | Model loop, normalized MCP adapter, guards, result contract |
| `src/prompts/apply_prompt.py` | Operational application policy and document ordering |
| `src/self_improvement_agent.py`, `src/self_improvement/` | Isolated diagnosis/patch control plane |
| `src/dashboard/` | Read-mostly FastAPI/htmx operator dashboard |
| `src/settings.py`, `src/models.py`, `src/store.py` | Typed config, domain records, SQLite state |
| `tests/` | Offline unit, harness, and opt-in browser-contract tests |
| `deploy/` | VPS provisioning, systemd, Caddy, LiteLLM, browser policy |
| `documents/`, `state/`, `logs/` | Private/runtime data; gitignored |

Coding agents should start with [`AGENTS.md`](AGENTS.md). All documentation is
indexed in [`docs/README.md`](docs/README.md).

## Prerequisites

- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node 20+ for agent-browser and the optional Claude CLI
- WSL2 + WSLg locally, or Xvfb on the VPS
- A DeepSeek API key
- Google Desktop OAuth credentials for Gmail

## Local setup

```bash
just sync
uv run playwright install chromium
just ensure-agent-browser
cp .env.example .env              # edit locally; never commit
just doctor
```

Create a Google Desktop OAuth client and save it as
`state/gmail_client_secret.json`. The first Gmail authorization stores
`state/gmail_token.json`.

Place personal application files in `documents/` using the naming guide in
[`documents/README.md`](documents/README.md). Everything except that README is
ignored by Git.

For one-time browser sign-ins:

```bash
just login
```

Sign into Google first, then Stekkies and rental sites in the opened persistent
browser. For non-SSO credentials, optionally import a Google Password Manager
CSV and delete the CSV immediately afterward:

```bash
just import-passwords passwords.csv
```

## Optional self-improvement service

Non-terminal failures can be diagnosed and patched in isolated Git worktrees.
The Claude Agent SDK is routed through a loopback LiteLLM proxy backed by the
same DeepSeek account.

```bash
just ensure-claude-cli
just litellm-proxy     # keep this terminal running locally
```

`just doctor` reports whether these optional dependencies are available. The
control plane, publish rules, and cost caveats are documented in
[`docs/architecture.md`](docs/architecture.md).

## Run

```bash
just host            # terminal/service 1: persistent shared Chromium
just once <url>      # real autonomous application for one listing
just watch           # real live Gmail watcher
```

Use `just dry-prompt <listing-json>` to render task text without opening a
browser. It can print private document filenames, so handle its output as
sensitive.

## Develop and verify

```bash
just --list
just docs-check
just check
```

`just check` is the one offline quality gate: documentation links, Ruff, `ty`,
byte compilation, pytest with coverage ratchet, apply-harness regression, and
import/prompt smoke. Task-specific test routing is in
[`docs/development.md`](docs/development.md).

The opt-in `just agent-browser-smoke` validates the real pinned MCP contract
against a disposable local page/profile. It is not part of ordinary unit
development.

## Observe and operate

- `logs/activity.log`: concise human-readable handled-listing events.
- `logs/mail_summary.jsonl`: structured trigger and result events.
- `logs/transcripts/`: per-run transcripts; treat as sensitive.
- `logs/trajectories/`: redacted structured per-turn evidence.
- `just dashboard`: local dashboard at `http://127.0.0.1:8000`.
- `just digest`: weekly-style outcome and self-improvement summary.

The dashboard includes submissions, funnel, health, self-improvement, and
redacted forensics. Its pause/resume/health actions affect services; its retry
action starts another real autonomous application.

For 24/7 VPS provisioning, CI/CD, VNC login, backups, and service operations,
see [`deploy/README.md`](deploy/README.md).
