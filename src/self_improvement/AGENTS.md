# Self-improvement guide

Scope includes this package and its public sibling facade
`src/self_improvement_agent.py`. The facade intentionally remains outside the
package for import compatibility, so edits often need to inspect both.

Read the [`control-plane failure lesson`](../../docs/lessons/2026-07-10-self-improvement-control-plane-failures.md)
before changing authority, tools, worktrees, verification, or publishing.

## Ownership

- `../self_improvement_agent.py`: trigger API, incident orchestration, two-phase
  SDK execution, authority callbacks, commit/push/recovery, and result parsing.
- `prompts.py`: diagnosis and patch instructions for apply and session failures.
- `worktree.py`: isolated worktree creation, cleanup, and orphan recovery.
- `browser_tools.py`: narrow read-mostly diagnostics over shared CDP under the
  browser lock.
- `cost.py`: DeepSeek token-based cost estimate; SDK dollar fields are wrong for
  the proxy model.
- `util.py`: context redaction helper.
- `../self_improvement_queue.py` and `../self_improvement_worker.py`: durable
  scheduling and single-worker execution.
- `../incident_store.py`: deterministic fingerprint and attempt history.

## Invariants

- Trigger paths enqueue; they do not run a repair inline with an application.
- One global worker flock prevents agents racing `main` or killing siblings
  during deployment.
- Every run uses a throwaway worktree based on freshly fetched `origin/main`,
  never the live checkout. Cleanup runs in `finally`; orphan recovery remains.
- Diagnosis is read-only and evidence-first. Only its authoritative tool/result
  can authorize a patch phase; free text is not authority.
- Diagnosis and patch have separate turn budgets. Do not collapse them into one
  model run.
- SDK `tools=` is the availability boundary; `allowed_tools` merely controls
  approval. Keep `can_use_tool` and internal path/command checks as independent
  enforcement.
- Patch writes stay inside the isolated worktree. Raw Git commit/push/reset is
  denied; only the dedicated commit/push/deploy tool publishes.
- `just check` must pass before any push. If fast-forward or deploy permission
  fails, use a review branch. If every push fails, persist a `git am` patch and
  notify; a verified fix must not disappear with the worktree.
- Known external gates should become `record_known_gate` data, not speculative
  code patches.
- Prompts and persisted context are redacted. Browser diagnostics are narrow and
  serialized; they do not gain submit/payment capability.
- LiteLLM on this path must not receive Claude `thinking`, `effort`, or
  `output_config.format`. Parse explicit text markers instead.
- Trust `_estimate_deepseek_cost_usd()` from raw usage, not SDK cost fields. The
  configured SDK max-budget value intentionally compensates for inflated
  client-side pricing.

## Verification

```bash
uv run pytest -q tests/test_self_improvement_agent.py \
  tests/test_self_improvement_reliability.py \
  tests/test_self_improvement_harness.py \
  tests/test_incident_store.py
just self-improve-eval
just check
```

Use synthetic/redacted fixture context. Do not execute the real repair agent or
publish a branch merely to validate control flow.
