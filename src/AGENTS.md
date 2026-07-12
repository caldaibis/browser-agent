# Source-tree guide

These instructions apply to Python runtime code under `src/`. Read the root
[`../AGENTS.md`](../AGENTS.md) first and the nearest package guide when one
exists.

Some public facades sit beside their implementation package. When editing
`browser_dom_tools.py`, `agent_tools.py`, or `site_fastpaths.py`, also read
[`browser_agent/AGENTS.md`](browser_agent/AGENTS.md). When editing
`self_improvement_agent.py`, its queue/worker, or `incident_store.py`, also read
[`self_improvement/AGENTS.md`](self_improvement/AGENTS.md). Prompt or message
changes also require [`prompts/AGENTS.md`](prompts/AGENTS.md).

## Boundaries

- `models.py` owns the typed pipeline records; do not grow parallel listing
  dictionaries in core code.
- `settings.py` owns runtime environment knobs. The deliberate exception is the
  applicant's `APPLICANT_*` profile in `applicant_profile.py`; do not create
  further direct environment parsers.
- `config.py` owns paths and fixed service URLs, not feature settings.
- `store.py` is the authoritative durable-state adapter. Consumers should not
  query `state/store.db` directly.
- `eventlog.py` and `redaction.py` own safe persisted output. New JSONL writers
  should use them rather than local timestamp/redaction helpers.
- `browser_agent/result.py` owns the apply result/outcome contract.
- `apply.py`, `orchestrator.py`, and `self_improvement_agent.py` are public
  facades and orchestration boundaries. Keep leaf mechanics out of them when a
  focused module already owns the concern.

## Implementation rules

- Use package-relative imports and run entry points with `python -m src.<name>`.
- Prefer frozen dataclasses for stable domain records and explicit
  `from_json()` / `to_json()` compatibility at untyped boundaries.
- Preserve old record parsing when changing persisted shapes; production state
  and logs outlive code versions.
- Keep fail-open helpers visibly isolated from the main verdict. Their exception
  handling should log enough to diagnose without leaking input data.
- Do not convert a safety check to fail-open. Duplicate, payment, browser-policy,
  and self-improvement authorization failures stop the protected action.
- Avoid import-time network/browser work. Some existing modules bind settings or
  create configured paths for compatibility; do not expand those side effects.
- Keep public/test-visible seams stable. Before moving a function, search tests
  for `patch()` targets and package re-exports.

## Verification

Use the task table in [`../docs/development.md`](../docs/development.md) for
focused tests, then run `just check`. A change to cross-cutting contracts needs
consumer tests, not only a unit test of the owner.
