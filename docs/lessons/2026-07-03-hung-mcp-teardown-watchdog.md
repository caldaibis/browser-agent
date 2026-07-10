# asyncio.wait_for cannot unwedge a hung MCP teardown

(from AGENTS.md, verbatim; see git history for context)

**`asyncio.wait_for` cannot unwedge a hung MCP teardown.** It only cancels
the task; the cancellation still unwinds `stdio_client.__aexit__`, which
waits on the npx process — if that ignores closed stdin, `asyncio.run()`
blocks forever holding the browser flock (03-07-2026: 9+ hours, eight
consecutive mail applies starved out at 1800s each). `run_agent` now arms a
`threading.Timer` watchdog that SIGKILLs wedged MCP descendants
`APPLY_TEARDOWN_GRACE_SECONDS` (120s) past the wall-clock timeout, and
`browser_lock` records its holder + pushes an alert after a 300s wait.
