# Self-improvement failures were control-plane failures (2026-07-10)

## Incident

Six of fifteen recent self-improvement sessions failed operationally. Several
contained a correct diagnosis or verified patch, but the surrounding flow lost
or misreported the result.

Observed causes:

- generic `ExceptionGroup` summaries discarded the nested causal error;
- success depended on a model-authored JSON marker after commit/push;
- `allowed_tools` was mistaken for an SDK availability allowlist;
- the model edited/staged the live checkout despite a worktree prompt;
- concurrent runs raced pushes and deployments restarted their parent services;
- browser diagnostics waited two minutes for optional evidence;
- `npx @playwright/mcp@latest` moved to Node 20 while the VPS remained Node 18.

## Resolution

- Persist failures to a redacted durable queue and drain it with one dedicated
  oneshot worker under a global flock.
- Recover claimed jobs and managed worktrees after process death. Record
  `run_started`, phase, authoritative tool, terminal, and abandonment events.
- Capture complete nested exception tracebacks before enqueueing and classify
  runtime/MCP infrastructure failures globally across domains.
- Use `submit_diagnosis` and commit/push tool state as authoritative results;
  model text markers are compatibility fallback only.
- Restrict built-in SDK tools with `tools=`, disallow task/subagent tools, deny
  writes outside the generated worktree, and expose fixed validation tools
  instead of patch-phase Bash.
- Fail browser-diagnostic lock acquisition after 10 seconds.
- Enforce Node 20+, pin Playwright MCP, and healthcheck a real MCP
  initialize/list-tools handshake in a killable subprocess.

## Standing rule

Do not solve repair-agent reliability by increasing turn limits alone. Give the
agent complete structured evidence, bounded tools, an early authoritative exit,
and a process lifecycle that cannot be killed by its own deployment.
