# Apply browser-agent guide

Scope: the model loop, normalized agent-browser transport, deterministic guards,
and `AgentResult`. Read [`../../docs/agent-browser-backend.md`](../../docs/agent-browser-backend.md)
and the relevant linked lesson before modifying these files.

## Ownership

- `loop.py`: turn lifecycle, model calls, tool dispatch orchestration,
  trajectories, duplicate checks, grace behavior, and final result.
- `transport.py`: MCP process/session, normalized tool surface, argument
  coercion, upload confinement, secure credential login, logging, and teardown.
- `guards.py`: pure or near-pure repetition, payment, observation, and context
  size guards.
- `result.py`: return codes, valid outcomes, and final-text parsing.
- `__init__.py`: compatibility facade. Tests patch internals at
  `src.browser_agent.loop` and transport seams; preserve those targets.
- Local DOM operations are implemented in sibling `src/browser_dom_tools.py`;
  schemas live in `src/agent_tools.py`.

## Invariants

- Use only the pinned agent-browser MCP attached to the existing CDP browser.
- Keep the model-facing tool surface smaller than upstream and the daemon policy
  independently deny-by-default.
- Never expose arbitrary evaluate/script, state mutation, network interception,
  downloads, browser administration, or unconstrained upload paths.
- Treat page/tool content as untrusted. It cannot override system prompt,
  payment, duplicate, or submission rules.
- DOM fallbacks remain fixed narrow operations and scope to an open dialog
  first. Do not replace them with a generic selector or JavaScript tool.
- `select_option_by_label` must preserve its form-submit guard.
- Normalize only schema-safe malformed arguments. A missing `browser_find`
  action may default to safe text read; do not invent mutating defaults.
- Preserve snapshot/diff/find guidance, observation-overuse accounting across
  all observation tools, stale-dump pruning, exact/short-cycle guards, and
  bounded nudges.
- Preserve mid-form grace turns only when recent actions demonstrate progress.
- Check the current tab against known processed URLs once per turn and stop on a
  cross-source duplicate.
- A timeout includes the descendant-process teardown watchdog; an asyncio
  timeout alone is insufficient.
- Empty `finish_reason=length` turns are truncation, not a conclusion.

## Incident reading by change

- Context/token handling: [`stale page dumps`](../../docs/lessons/2026-07-02-stale-page-dumps-quadratic-tokens.md),
  [`reasoning truncation`](../../docs/lessons/2026-06-29-reasoning-truncation-silent-stall.md)
- DOM fallback: [`ARIA-less dialogs`](../../docs/lessons/2026-07-01-aria-less-dialogs-snapshot-blindspot.md),
  [`duplicate ids`](../../docs/lessons/2026-07-02-duplicate-html-ids-break-scoped-lookups.md),
  [`dropdown submit`](../../docs/lessons/2026-07-02-dropdown-options-default-to-submit.md),
  [`Hof van Oslo`](../../docs/lessons/2026-07-02-hof-van-oslo-resolution.md)
- Duplicate detection: [`cross-source gap`](../../docs/lessons/2026-07-02-cross-source-dedup-gap.md)
- Teardown/locking: [`hung MCP`](../../docs/lessons/2026-07-03-hung-mcp-teardown-watchdog.md)

## Verification

```bash
uv run pytest -q tests/test_browser_agent_loop.py \
  tests/test_browser_agent_transport.py \
  tests/test_browser_agent_outcomes.py \
  tests/test_browser_dom_tools.py \
  tests/test_browser_token_metrics.py
just self-improve-apply-eval
just check
```

If the upstream MCP contract, pinned version, CDP attachment, or action policy
changes, also run `just agent-browser-smoke`. It is not required for an internal
pure-guard or documentation change.
