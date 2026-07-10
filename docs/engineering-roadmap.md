# Engineering roadmap — substrate-quality overhaul

Decided 08-07-2026 after a high-level technical review. Goal: raise the
software substrate (types, config, logging, storage, structure, tooling) to
match the operational quality this repo already has. Each item below is
specified precisely enough to implement in a fresh session with no other
context. **Update the Status column as items land.**

| # | Item | Status |
|---|------|--------|
| R1 | Typed domain model (`src/models.py`) | done (08-07-2026: `Listing` + `ProcessedRecord` with centralized `dedup_keys()`/`keys()`; apply path fully typed, `to_json()` at fan-out boundaries — pushing the type into playbooks/SI contexts is follow-up) |
| R2 | Rust-built type checker in `just check` | done (08-07-2026: ty 0.0.57 gates `src/`; ruff widened to F,B,UP,ASYNC,SIM) |
| R3 | Central typed settings module | done (08-07-2026: `src/settings.py`, all 60+ knobs; `just settings` prints resolved values; APPLICANT_* stays in applicant_profile.py by design) |
| R4 | Structured logging + shared event log + UTC timestamps | done (08-07-2026: `src/eventlog.py` + `src/redaction.py`; all persisted stamps UTC-aware; readers accept both forms via `eventlog.parse_ts`; service prints → shared logger) |
| R5 | SQLite state store | done (08-07-2026: `src/store.py` — processed listings + dedup keys + incidents; one-time JSONL migration; dual-write + union reads while soaking, flip reads to store-only in a later session; mail events deliberately left as a log file) |
| R6 | Split the two monoliths; prompts out of code | done (08-07-2026: apply prompt → `src/prompts/` (byte-identical output); SI agent 1607→1065 lines + `src/self_improvement/{prompts,worktree,cost,browser_tools,util}.py`; browser_agent tool schemas → `src/agent_tools.py`. Full package-split of browser_agent's loop deferred — tests patch its module globals; do it with a test refactor) |
| R7a | pytest as the one test runner + coverage ratchet | done (08-07-2026: pytest-cov, fail_under=55, floor noted in pyproject) |
| R7b | CI/deploy hardening (SHA pinning, host-key pinning, rollback) | done (08-07-2026: actions SHA-pinned; deploy uses `VPS_HOST_KEY` secret when set (loud warning + keyscan fallback until the user adds it); smoke check (units active + dashboard HTTP 200 on :8000) with automatic `git reset` rollback + restart on failure) |
| R7c | Real package (installable, entry points) | deferred by design (08-07-2026 — requires the `src/`→`stekkies/` rename; full migration recipe in the R7c section below) |
| R7d | AGENTS.md restructure (lessons → `docs/lessons/`) | done (08-07-2026: 13 dated postmortems moved verbatim; AGENTS.md keeps the standing rules + links and got its architecture/conventions text updated for the new substrate) |

## R1 — Typed domain model

**Problem.** The pipeline's core currency (a listing) is a bare `dict` with
stringly-typed keys defensively `.get()`-ed everywhere; every JSONL record
type (processed listings, mail summaries, poll events) re-invents its schema
by convention at each producer/consumer. The dedup sagas (Kaatstraat, Hof van
Oslo) were at root "same entity, several ad-hoc key representations" bugs.

**Fix.** `src/models.py` with frozen dataclasses + `from_json`/`to_json`:
`Listing` (source_url required; address/price/source_name/description/
stekkies_url optional), `ProcessedRecord`, `MailSummaryRecord`. `Listing`
centralizes identity: a `dedup_keys()` method returning every key form
(raw + canonical, source/stekkies/resolved) so key-derivation logic lives in
ONE place. Migrate call sites incrementally: `apply.build_prompt`/`apply.apply`,
orchestrator, poller watcher/enqueue, dashboard readers. Producers write via
`to_json`; readers accept unknown keys (forward compat).

## R2 — Type checker (rust-built: ty or pyrefly)

Wire a rust-built checker (`ty` or `pyrefly` — decide empirically: run both,
keep the one whose findings are tractable on this codebase) into
`just check` as a hard gate, since `just check` is also the self-improvement
agent's verify command: every gate here directly constrains machine-authored
patches. Config in `pyproject.toml`; start permissive (silence rules with a
high false-positive rate on this codebase), tighten over time. Also widen
ruff to bug-catching (not style) rule sets: `B`, `UP`, `SIM`, `ASYNC`.

## R3 — Central typed settings

**Problem.** ~77 module-level `os.environ` reads; no single inventory of the
system's knobs; malformed values crash at first-import instead of failing
fast; tests must patch module attributes; `config.py` has import side effects.

**Fix.** `src/settings.py`: one frozen dataclass `Settings` loaded once
(`settings()` accessor with a `reload_settings()` test hook), grouping every
env knob with type, default, and a one-line comment. Modules read
`settings().apply_max_turns` etc. at call time (not import time) so tests and
env changes behave. Validation happens in one place with a clear error naming
the offending variable. `just doctor` prints the resolved settings.

## R4 — Structured logging, shared event log, UTC

**Problem.** 85 `print()` calls; ≥3 duplicated `_log`/`_activity` JSONL-append
helpers; naive-local-time timestamps everywhere (DST hazard, mismatch with
UTC journald); redaction enforced only dashboard-side.

**Fix.** `src/eventlog.py`: `utc_now_iso()`, `append_jsonl(path, record)`,
`log_event(path, event, **fields)`, `activity(message)` — the one place that
stamps timestamps (UTC, `+00:00` offset) and applies `redact()` before
anything is written. Stdlib `logging` with a console handler replaces bare
prints (journald captures stdout, so ops-transparent). All readers must keep
accepting old naive timestamps (fromisoformat handles both).

## R5 — SQLite state store

**Problem.** A dozen JSON/JSONL files appended by four processes and read by
the dashboard; dedup logic (multi-key canonicalization at load time, tail
caches) compensates for the lack of a queryable store.

**Fix.** `src/store.py`: one SQLite DB `state/store.db` (WAL mode,
`busy_timeout`), schema: `processed_listings` (rowid, ts, json payload) +
`listing_keys` (key TEXT PRIMARY KEY, canonical, listing rowid) for O(1)
multi-key dedup; `incidents`; `mail_events`. Transparent one-time migration:
on first open, import existing JSONL files (idempotent, keyed). **Writers
dual-write** (SQLite + legacy JSONL append) for one release so a rollback
loses nothing; readers prefer SQLite. Boundary rule: *state* (dedup keys,
processed records, incidents) → SQLite; *logs* (trajectories, poller.jsonl,
transcripts) stay append-only files — logs are logs.

## R6 — Split the monoliths; prompts out of code

`self_improvement_agent.py` (1.6k lines) → package `src/self_improvement/`
(`worktree.py`, `mcp_tools.py`, `prompts.py`, `cost.py`, `agent.py` facade
re-exporting the public API so imports keep working).
`browser_agent.py` (1.2k) → `src/browser_agent/` (`loop.py`, `guards.py`,
`transport.py`, facade `__init__.py`). `apply.build_prompt`'s ~190-line
f-string → `src/prompts/apply_prompt.py` with named clause builders, testable
as text. Keep behavior byte-identical where tests assert on prompt content.

## R7a — One test runner + coverage ratchet

`just check` runs `uv run pytest` (not `unittest discover`); existing
`unittest.TestCase` classes work under pytest unchanged. Add `pytest-cov`;
`just check` enforces `--cov=src --cov-fail-under=<current floor>` — a soft
ratchet: raise the floor when coverage rises, never lower it.

## R7b — CI/deploy hardening

- Pin all GitHub Actions to full commit SHAs.
- Host-key pinning: `VPS_HOST_KEY` secret (content of `ssh-keyscan -H <host>`)
  written to known_hosts; fall back to live keyscan with a loud warning if
  unset.
- Rollback: deploy records the pre-pull SHA; after restart, a smoke check
  (systemd active + dashboard HTTP 200 via the healthcheck) must pass or the
  script resets to the recorded SHA, re-syncs, restarts, and exits nonzero.

## R7c — Real package

**Evaluated and deliberately deferred (08-07-2026).** A clean installable
package requires renaming the import package (`src/` → e.g. `stekkies/`):
setuptools mis-detects a package literally named `src` as src-layout (it
would install `poller`/`dashboard`/`prompts` as colliding top-level
packages), and an editable install named `src` pollutes site-packages.
The rename touches the live VPS systemd units (`python -m src.orchestrator`),
`deploy/ensure-self-improvement.sh`, the justfile, the self-improvement
agent's prompts (they name `src/poller/parsers.py` paths), and AGENTS.md —
do it in a dedicated session, ideally alongside a VPS re-provision, as:
(1) `git mv src stekkies`; (2) sed `src.` → `stekkies.` in justfile, deploy
units/scripts, tests, docs, SI prompts; (3) `[project] name`,
`package = true`, console entry points; (4) deploy + verify all units.
Tooling (pytest/ruff/ty/uv) already works fully against the current layout,
so the only unrealized benefit is entry points.

## R7d — AGENTS.md restructure

Postmortems → `docs/lessons/YYYY-MM-DD-<slug>.md` (one dated file per
incident, verbatim content). AGENTS.md keeps the lean architecture map,
conventions, gotchas, and a linked index of lessons. Shrinks per-session
agent context while keeping the crown jewels durable.

## Follow-ups queued for later sessions

- **Flip store reads to SQLite-only + drop dual-writes** after a soak
  release: `orchestrator._processed_keys` / `dedup._processed_urls_canonical`
  drop the JSONL union; `incident_store._read` prefers the store; then retire
  the JSONL appends.
- **Add the `VPS_HOST_KEY` GitHub secret** (output of `ssh-keyscan <host>`)
  to activate deploy host-key pinning; until then deploy warns and keyscans.
- **R7c rename** (`src/` → `stekkies/`) per the recipe above, ideally with a
  VPS re-provision.
- **browser_agent loop package-split** (see R6 note) together with a
  test_browser_agent_loop.py refactor away from module-global patching.
- **Push `Listing` deeper**: site_playbooks / self-improvement contexts /
  incident fingerprints still take `listing.to_json()` dicts at the boundary.
- Raise the coverage ratchet as coverage rises (60% at session end vs 55 floor).

## Session log

- 08-07-2026: roadmap created; R1–R6, R7a, R7b, R7d implemented and verified
  (`just check` green, 260 tests, coverage 60%); R7c evaluated and deferred
  with a migration recipe. All work left uncommitted for human review.
