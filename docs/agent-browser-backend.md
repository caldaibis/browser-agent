# agent-browser apply backend

The production apply loop uses agent-browser's native CDP daemon and MCP server,
pinned by `deploy/agent-browser.version`. It attaches to browser-host's existing
Chromium on port 9222; it does not launch or own the shared browser profile.

## Why the interface is normalized

The upstream `all` MCP profile is required for uploads, semantic locators,
dialogs, stable tabs, and snapshot diffs, but it also contains JavaScript,
browser shutdown, state/cookie mutation, downloads, network interception,
plugins, installation, upgrade, and AI chat. `src/browser_agent/transport.py`
publishes only the operations the apply model needs under stable `browser_*`
names. The model cannot set `extraArgs`, sessions, namespaces, restore options,
or output paths.

`deploy/agent-browser-action-policy.json` is a second, daemon-enforced boundary.
Its allowlist uses the pinned runtime's concrete action names and denies
everything except the normalized navigation, interaction, upload, observation,
tab, dialog, auth-vault, and safe-getter operations. Arbitrary JavaScript
remains denied even if a future tool-filter regression exposes it.

The MCP server invokes agent-browser commands internally, so CDP, namespace,
output-cap, and policy configuration is passed through inherited
`AGENT_BROWSER_*` environment variables rather than top-level MCP process
flags. v0.31.1 does not add content-boundary markers to MCP snapshot results;
the adapter therefore wraps every site-controlled result itself and leaves the
upstream option enabled for forward compatibility.

## Features used

- Compact interactive snapshots are the default and include link URLs.
- Full, depth-limited, and CSS-scoped snapshots expose eligibility, validation,
  and confirmation text without dumping unrelated page structure.
- Snapshot diffs verify same-page changes without another full page dump.
- Stable tab IDs handle SSO and backing-portal popups.
- Semantic role/text/label/placeholder lookup is the first fallback for
  elements without refs.
- The existing open-dialog-scoped DOM tools remain for duplicate IDs,
  accessibility-blind dialogs, and custom dropdowns.
- Upload paths are resolved and rejected unless they are files below `DOCS_DIR`.
- Credentials are copied into agent-browser's encrypted auth vault only inside
  the local tool call, then its login operation fills/submits the current login
  URL. Passwords never appear in the LLM prompt, tool result, transcript, or
  trajectory.
- Content boundary markers distinguish untrusted page text from task
  instructions. The prompt explicitly tells the model never to obey page-borne
  instructions.
- Output is capped by `AGENT_BROWSER_MAX_OUTPUT_CHARS` and again by the apply
  loop's marked truncation guard.
- Overlay interception diagnostics and page errors are available without raw
  JavaScript.
- Tool-call arguments DeepSeek emits as strings (e.g. `interactive="True"`)
  are coerced to each tool's own declared boolean/integer/number schema type
  before dispatch, and a `browser_find` call missing `action` defaults to the
  read-only `"text"` instead of erroring.
- `aggregator_hop` is a composite fast path for the huurwoningen.nl gateway
  ("Contact met de verhuurder" control -> "Ga verder" dialog) so the model
  doesn't rediscover that two-click flow by hand every run.

Annotated screenshots, streaming, React introspection, mobile providers,
cloud browsers, state restore, and its embedded AI chat are intentionally not
used. The apply model is text-only, browser-host already owns persistent state,
the dashboard/VNC cover observation, and extra providers or agent loops would
add failure and security surfaces without helping rental submission.

## Operations

Install or repair the exact version:

```bash
just ensure-agent-browser
just doctor
```

Run the real contract test against a disposable page/profile:

```bash
just agent-browser-smoke
```

Generate deterministic before/after contract metrics (no LLM, website, or
timing measurements):

```bash
just browser-backend-metrics
just browser-backend-metrics /tmp/browser-backend-metrics.json
```

The report compares the complete tool contract sent to the apply model for the
pinned Playwright rollback backend and agent-browser. It measures canonical
schema bytes, exposed tool/risk counts, workflow capabilities, and whether a
password can be returned to model context. These are leading engineering
metrics, not evidence of a higher submission rate; production outcomes must be
measured after deployment.

Current pinned result (10-07-2026):

| Deterministic measure | Playwright baseline | agent-browser | Change |
| --- | ---: | ---: | ---: |
| Tool-contract bytes per model request | 18,795 | 11,312 | -39.81% |
| Total apply tools exposed | 27 | 27 | 0 |
| Audited workflow capabilities | 8 | 12 | +4 |
| Risk tools exposed | 1 (`browser_close`) | 0 | -1 |
| Password can be returned to model | yes | no | removed |

The command fails unless the candidate contract is smaller, its capability set
is a strict superset, it adds no risk tools, and plaintext credential delivery
to the model is removed. Capability definitions are an auditable mapping from
named workflow requirements to exact tool names in
`src/browser_backend_metrics.py`.

### Session token cohorts

Token consumption is empirical rather than deterministic: it depends on the
listing, site, model output, and outcome. Its calculation is deterministic from
the recorded trajectories:

```bash
just browser-token-metrics
just browser-token-metrics /path/to/copied/trajectories
```

The report groups by browser backend, backend version, and model; sums the
provider-reported per-turn tokens for each session; and reports mean, median,
p90, min, and max for prompt/completion/total/reasoning/cache tokens and turns.
It separately reports tokens per submitted session and marks a comparison ready
only when at least two cohorts have 10 or more sessions. Runs written before the
backend field existed are explicitly classified as legacy Playwright. Sessions
with missing provider token usage remain in outcome counts but are reported and
excluded from token distributions; totals are also broken down by outcome and
for non-yielded sessions to make cohort-mix differences visible.

Emergency rollback requires no code change:

```bash
APPLY_BROWSER_BACKEND=playwright just once <listing-url>
```

Do not change `deploy/agent-browser.version` without running the unit suite,
`just agent-browser-smoke`, and `just check`. Its MCP surface is validated at
startup and a missing required upstream tool fails before the model acts.
