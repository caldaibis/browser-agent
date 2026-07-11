# agent-browser apply backend

The apply loop uses only agent-browser's native CDP daemon and MCP server,
pinned by deploy/agent-browser.version. It attaches to browser-host's existing
Chromium on port 9222 and never owns the shared browser profile.

The upstream all profile contains more operations than the apply agent needs.
src/browser_agent/transport.py publishes a small normalized browser_* surface,
while deploy/agent-browser-action-policy.json independently denies JavaScript,
browser shutdown, state/cookie mutation, downloads, network interception,
plugins, installation, upgrades, and chat tools.

The adapter uses compact snapshots, scoped snapshots, snapshot diffs, semantic
find, stable tabs, waits, dialogs, secure credential-vault login, and the
open-dialog-scoped DOM fallbacks. Uploads are confined to DOCS_DIR, and
site-controlled results are marked as untrusted page content.

Install or repair the exact version:

~~~text
just ensure-agent-browser
just doctor
~~~

Run the real contract test against a disposable page/profile:

~~~text
just agent-browser-smoke
~~~

The browser backend is intentionally not configurable: removing the old
legacy environment variable or stale rollback invocation cannot start a second
browser tool contract.

Token consumption remains empirical and is reported by:

~~~text
just browser-token-metrics
just browser-token-metrics /path/to/copied/trajectories
~~~

Runs written before the backend field existed are classified as legacy/unknown;
they are retained for historical outcome and token analysis only.

Do not change deploy/agent-browser.version without running the unit suite,
just agent-browser-smoke, and just check.
