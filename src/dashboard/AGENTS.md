# Dashboard guide

The dashboard is a read-mostly FastAPI/htmx operator interface. It reads live
SQLite/log/state files produced by other processes and exposes a small,
explicit set of POST actions.

## Ownership

- `app.py`: routes, lifespan/cache warmer, templates, and explicit service/retry
  actions.
- `data.py`: submission records, formatting, transcript lookup, mission KPIs.
- `trajectories.py`: structured per-turn timeline with transcript fallback.
- `costs.py`: apply/SI token usage and cost rollups.
- `funnel.py`: mail/outcome/incident funnel aggregates.
- `si.py`: self-improvement runs, incidents, gates, patches, and playbooks.
- `healthinfo.py`: service/login/credit/attention state.
- `cache.py`: incremental JSONL tail and TTL memoization.
- `templates/` and `static/`: presentation only; do not recreate business
  calculations in Jinja or JavaScript.

## Security and compatibility

- Treat every transcript, playbook, log, and state value as sensitive and
  untrusted. Apply `data.redact()`/shared redaction before rendering, including
  error paths and snippets.
- Never serve `*.prompt.txt`, arbitrary filesystem paths, raw credentials,
  application documents, browser profile data, or unrestricted pending files.
- Validate names/domains and resolve paths inside their expected directory
  before reading.
- GET routes remain read-only. New mutations require an explicit POST route,
  bounded arguments, operator feedback, and a narrowly scoped backend action.
- Preserve legacy timestamp and submission-permalink compatibility; dashboards
  read records written by older code versions.
- Keep expensive file scans behind `JsonlTail` or memo caches. One overview
  request must not repeatedly parse full append-only logs.
- The dashboard is not an authoritative state writer except for deliberate safe
  actions such as known-gate removal and retry orchestration.
- A dashboard retry is a real autonomous application attempt; never invoke it
  from tests or health probes.

## Verification

```bash
uv run pytest -q tests/test_dashboard_attention.py \
  tests/test_dashboard_cache.py \
  tests/test_dashboard_funnel.py \
  tests/test_dashboard_si.py \
  tests/test_dashboard_tokens.py \
  tests/test_dashboard_trajectories.py
just check
```

Use FastAPI/test-level rendering and synthetic temporary files. Starting the
local dashboard is optional visual validation, not a substitute for assertions.
