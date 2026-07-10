# asyncio's default executor is tiny and DNS shares it

(from AGENTS.md, verbatim; see git history for context)

**asyncio's default executor is tiny and DNS shares it.** `asyncio.to_thread`
AND `loop.getaddrinfo` both use the loop's default executor (8 threads on a
4-vCPU box). ~13 tier-3 watchers parking threads on the browser flock
starved DNS, so every pending httpx connect timed out AT ONCE — 10k+
ConnectTimeout poll_errors/day, ~80% of tier-2 polls silently lost
(diagnosed 07-07-2026 from all-sites-simultaneous timeout bursts). Fixes:
a 64-thread default executor (`POLL_EXECUTOR_THREADS`), tier-3 polls give
up on the lock after `POLL_TIER3_LOCK_TIMEOUT` (120s) instead of queueing
30 min, and startup polls are staggered.
