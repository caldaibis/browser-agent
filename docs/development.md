# Development guide

This guide routes a change to its canonical owner and proportionate checks. It
complements the global constraints in [`../AGENTS.md`](../AGENTS.md) and the
system map in [`architecture.md`](architecture.md).

## First five minutes

```bash
git status --short
just --list
just doctor        # environment/runtime preflight; safe but may report missing services
just docs-check    # documentation-only focused check
```

Before editing, inspect the public caller, implementation, tests, and nearest
`AGENTS.md`. Do not start the shared browser or a live application simply to
understand a unit-testable path.

## Command safety

| Category | Commands | Effect |
|---|---|---|
| Safe/read-only | `just`, `just settings`, `just docs-check`, focused pytest, `just check`, `just dry-prompt` | No external mutation; `dry-prompt` may make a fail-open listing GET and print private document filenames |
| Local services | `just host`, `just dashboard`, `just litellm-proxy`, `just login` | Starts processes; `login` opens real sites |
| Real application | `just once <url>`, `just watch`, dashboard retry | Can upload documents and submit a rental application |
| External mutation | `just deploy`, `just pause`, `just resume`, `just restart`, secret-push recipes, Git push recipes | Changes VPS, Git remote, services, or secrets |

`tests/test_agent_browser_live.py` uses a disposable page/profile, but it still
requires the pinned external runtime. Run `just agent-browser-smoke` only for a
browser-contract change.

## Task routing and focused validation

Always finish with `just check`. The commands below are fast feedback before
that full gate.

| Change | Read/edit owner | Focused checks |
|---|---|---|
| Gmail matching, MIME decoding, mail state | `src/gmail_watch.py`, `src/orchestrator.py` | `uv run pytest -q tests/test_gmail_watch.py tests/test_orchestrator_dedup.py` |
| Listing fields or serialization | `src/models.py` | Store, dedup, orchestrator, and prompt tests |
| URL identity/dedup | `src/dedup.py`, model key methods, store | `uv run pytest -q tests/test_dedup.py tests/test_store.py tests/test_orchestrator_dedup.py` plus linked dedup lessons |
| Rent/bedroom/payment veto | policy module and `src/apply.py` | matching policy tests and apply harness |
| Apply prompt or document ordering | `src/prompts/apply_prompt.py`, `src/message_template.py` | `uv run pytest -q tests/test_message_template.py tests/test_browser_agent_outcomes.py` and `just self-improve-apply-eval` |
| Browser tools or adapter | `src/browser_agent/`, `src/browser_dom_tools.py`, `src/agent_tools.py` | browser-agent transport/loop/DOM tests; live smoke if contract changed |
| Loop guard or token behavior | `src/browser_agent/guards.py`, `loop.py` | loop, outcome, token-metric tests and apply harness |
| Site-specific deterministic behavior | `src/site_fastpaths.py`, playbooks/gates | relevant unit tests and apply harness fixture |
| Session repair | `src/session_keeper.py` | `uv run pytest -q tests/test_session_keeper.py` |
| Settings/defaults | `src/settings.py`, `.env.example` | `uv run pytest -q tests/test_settings.py` and `just settings` |
| Store/event schema | `src/store.py`, `src/eventlog.py`, readers | store tests plus dashboard/digest consumers |
| Self-improvement control plane | `src/self_improvement_agent.py`, `src/self_improvement/` | self-improvement agent/reliability tests and `just self-improve-eval` |
| Failure classifier/trajectory | `src/self_improvement_harness.py`, fixtures | harness, loop, trajectory tests and both offline eval recipes |
| Dashboard data or routes | `src/dashboard/` | dashboard tests for data/cache/funnel/SI/attention/trajectories |
| Health, digest, notifications | named module | corresponding test module(s) |
| Deployment/systemd | `deploy/`, `.github/workflows/`, `justfile` | shell syntax where relevant, `just --list`, `just check`; no deploy without authorization |
| Documentation/navigation | `AGENTS.md`, nested guides, `docs/`, README files | `just docs-check` |

## Cross-cutting change recipes

### Add or change a runtime setting

1. Add the typed field to `Settings` and parse it in `load_settings()`.
2. Put aliases only in `src/settings.py`; do not add direct `os.environ` reads.
3. Read through `settings()` at the appropriate boundary. Preserve existing
   module constants when tests or imports depend on that seam.
4. Add valid, invalid, default, and alias coverage in `tests/test_settings.py`.
5. Update `.env.example` if an operator is likely to configure it. The complete
   machine-readable inventory remains `just settings` / `src/settings.py`.

### Add or rename an apply outcome

Treat this as a schema migration, not a local string change. Audit:

- `src/browser_agent/result.py` parsing and valid set;
- prompt final-output instructions and harness fixtures;
- orchestrator processing/mail behavior, especially `no_credit`;
- notification filters and self-improvement outcome settings;
- dashboard/funnel/digest grouping and historical-record compatibility.

Never silently reinterpret historical outcome strings.

### Add site-specific behavior

Choose the least powerful durable mechanism:

1. A playbook for learned navigation mechanics.
2. A known gate for an external account/payment/eligibility condition.
3. A narrow deterministic fast path when the flow is stable and measurable.
4. A normalized browser tool only when several sites need the capability.

Do not add generic JavaScript or expose more upstream MCP tools to solve one
site. Add a regression fixture based on redacted evidence.

### Change persisted data

- Durable state belongs in `src/store.py`; include migration/compatibility on
  connect and test both old and new shapes.
- Operational evidence remains append-only and uses `src/eventlog.py`.
- Apply redaction before write. Assume transcripts can still contain passwords
  and keep dashboard presentation redaction as defense in depth.
- Dashboard caches and historical readers must tolerate records written before
  the change.

### Change the apply prompt

Prompt wording is application behavior. Keep policy clauses in
`src/prompts/apply_prompt.py`, applicant wording in `src/message_template.py`,
and tool schemas in `src/agent_tools.py`. Do not duplicate a policy across all
three. Render with `just dry-prompt <listing-json>` when private local inputs
are acceptable, and keep prompt/harness regression assertions intentional.

### Change self-improvement

Preserve the separation between trigger, incident dedup, durable queue,
read-only diagnosis, isolated patch, verification, and publish/recovery. A model
instruction is not an enforcement boundary: tool availability, `can_use_tool`,
worktree path checks, and the dedicated commit tool must agree.

## Test structure

- Unit tests mirror source concerns as `tests/test_<module-or-contract>.py`.
- Cross-module safety regressions live with the contract they protect, not with
  whichever helper happened to expose the bug.
- `tests/fixtures/apply_harness_eval/` checks prompt/agent policy without a live
  browser.
- `tests/fixtures/self_improvement_harness/` checks deterministic failure
  classification.
- `tests/fixtures/browser_agent/` supports the opt-in real MCP regression.
- Keep fixtures synthetic and redacted. Never copy production transcripts,
  credentials, addresses, or document names into Git.
- Coverage is a ratchet in `pyproject.toml`: raise it when sustained coverage
  increases; never lower it to land a change.

## Quality gate

`just check` deliberately runs all of the following because autonomous patches
can deploy after it passes:

1. Documentation-link validation.
2. Ruff bug-catching rules.
3. `ty` over `src/`.
4. Byte compilation.
5. Pytest with coverage floor.
6. Apply-harness evaluation.
7. Import and prompt smoke.

If a focused check and `just check` disagree, the full gate wins. Do not weaken
a gate, mark a flaky safety test xfail, or lower coverage as a workaround.

## Documentation placement

- Current global constraint: root `AGENTS.md`.
- Scoped coding instruction: closest nested `AGENTS.md`.
- Current design and ownership: `docs/architecture.md`.
- Workflow or task-routing rule: this file.
- Production incident evidence: `docs/lessons/` plus a standing rule in the
  relevant agent guide.
- Operator setup: root or deploy README.
- Future behavior: `docs/planned-features.md`.
- Completed migration record: `docs/engineering-roadmap.md`.

Use relative Markdown links for internal references. `scripts/check_docs.py`
validates their targets and reports the source file and line.
