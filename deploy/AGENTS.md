# Deployment guide for coding agents

Files here provision and operate the real VPS. Read [`README.md`](README.md)
and the root [`../AGENTS.md`](../AGENTS.md) before editing.

## Runtime topology

- `browser-host.service` owns shared Chromium; `xvfb.service` supplies its VPS
  display.
- `orchestrator.service` watches mail and performs real applications.
- `dashboard.service` serves loopback FastAPI behind Caddy.
- `litellm-proxy.service` exposes the loopback Anthropic-shaped DeepSeek proxy.
- `healthcheck.timer` monitors credit, services, sessions, and repair health.
- `self-improvement-worker.timer` drains durable jobs separately from deploy
  restarts.
- `vnc.service` is for temporary interactive login and should otherwise stay
  stopped.

## Invariants

- Keep CDP and LiteLLM loopback-only. The public dashboard remains behind Caddy
  HTTPS and Basic Auth; VNC remains SSH-tunneled.
- Both orchestrator and dashboard load runtime values from `state/agent.env`.
  Preserve systemd `EnvironmentFile=` behavior.
- Deploys must verify the pinned agent-browser runtime and self-heal the Claude
  CLI/LiteLLM service expectations through `ensure-self-improvement.sh`.
- Do not restart `self-improvement-worker.timer` as part of an application
  deploy; isolation prevents a self-deploy killing a sibling repair.
- CI/CD actions stay SHA-pinned. Preserve host-key pinning, fast-forward pulls,
  post-restart smoke checks, and rollback to the previous revision.
- Never print environment contents or secret values. `state/`, `documents/`,
  credentials, Gmail tokens, VAPID keys, and browser profiles survive deploys.
- Before any destructive reset of an older VPS checkout, back up both
  `documents/` and `state/`; historical Git trees may still track documents.
- Run repository Git operations as the deploy user so ownership remains valid.
- A push to `main` is a deploy trigger after CI. Do not test a workflow edit by
  pushing or running `just deploy` without explicit authorization.

## Validation

- Parse/check shell files without executing provisioning or service mutation.
- Inspect unit dependencies and environment-file paths together when changing a
  service.
- Run `just --list`, relevant offline tests, `just docs-check`, and `just check`.
- Changes to the agent-browser pin or MCP contract additionally require
  `just agent-browser-smoke` in a suitable local environment.
