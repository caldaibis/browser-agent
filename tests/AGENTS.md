# Test and fixture guide

Tests must remain offline, deterministic, synthetic, and safe by default.

## Rules

- Mirror the contract under test, not an implementation accident. Production
  incidents should become the smallest regression that proves the invariant.
- Patch where the code looks up a name. Browser-agent tests often patch
  `src.browser_agent.loop`; package re-exports are not always the active seam.
- Use `tmp_path`, monkeypatch, and dependency seams for state, logs, settings,
  subprocesses, Gmail, browser, and network behavior.
- Never read the real `state/`, `logs/`, `.env`, browser profile, credential
  store, or `documents/` contents in a unit test.
- Never submit a form, send email/push, mutate systemd, contact a live rental
  site, push Git, or start the real self-improvement agent in the default suite.
- Keep network/live tests behind an explicit opt-in flag. The existing
  `RUN_AGENT_BROWSER_LIVE=1` test uses a disposable local HTML page and browser
  profile.
- Fixtures must contain invented identities, URLs, credentials, and listing
  details. Redact evidence before converting an incident into a fixture.
- Historical readers need old-shape tests when schemas or timestamps evolve.
- Do not lower the coverage floor. Add meaningful assertions for new branches;
  avoid tests whose only assertion is that a function did not raise.

## Fixture ownership

- `fixtures/apply_harness_eval/`: autonomous prompt/tool policy and hard stops.
- `fixtures/self_improvement_harness/`: deterministic weakness classification.
- `fixtures/browser_agent/`: disposable page for the opt-in real MCP contract.

The full gate is `just check`. See the routing matrix in
[`../docs/development.md`](../docs/development.md) for focused commands.
