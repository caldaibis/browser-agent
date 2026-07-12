# Documentation map

Use this page to choose context deliberately. The repository contains both
living documentation and historical evidence; reading all of it before a task
usually obscures the current design.

## Start here

| Document | Status | Read when |
|---|---|---|
| [`../AGENTS.md`](../AGENTS.md) | Living contract | Any coding-agent task |
| [`architecture.md`](architecture.md) | Living design | Locating ownership, tracing data, changing a boundary |
| [`development.md`](development.md) | Living workflow | Choosing tests, settings, logs, outcomes, or a safe command |
| [`../README.md`](../README.md) | Operator overview | Setting up or running the project |

The nearest nested `AGENTS.md` adds scoped instructions for `src/`, the browser
agent, prompts, self-improvement, dashboard, tests, and deployment. The root
guide links each one.

## Operations and interfaces

| Document | Scope |
|---|---|
| [`agent-browser-backend.md`](agent-browser-backend.md) | Pinned apply-browser contract and live smoke test |
| [`../deploy/README.md`](../deploy/README.md) | VPS provisioning, services, CI/CD, and operations |
| [`../documents/README.md`](../documents/README.md) | Private application-document naming and priority |

## Decisions and history

| Document | Status | Purpose |
|---|---|---|
| [`lessons/README.md`](lessons/README.md) | Incident index | Production failures and the invariants they created |
| [`engineering-roadmap.md`](engineering-roadmap.md) | Historical record | Completed substrate overhaul and deliberately deferred package rename |
| [`planned-features.md`](planned-features.md) | Planning | Ideas that are not current application behavior |

## Maintenance rules

- Keep current behavior in the living guides; keep chronology and evidence in
  lessons or the roadmap.
- Link to a canonical owner instead of copying its defaults or schema into
  several documents.
- Update architecture and the relevant nested guide in the same change that
  moves ownership or changes a cross-process contract.
- Run `just docs-check` after moving or renaming documentation.
