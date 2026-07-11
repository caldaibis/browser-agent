"""Exclusive lock on the shared CDP browser.

Only ONE component may drive the browser via the agent-browser MCP at a time. The
applier (apply.py) holds this for a whole submission; healthcheck's site
probes and the self-improvement agent's browser diagnostics must acquire it
before touching the browser and yield while it is held.

Backed by an OS file lock (fcntl.flock) so it is exclusive ACROSS PROCESSES —
the orchestrator, healthcheck, and self-improvement worker are separate
systemd services sharing one browser, and an in-process asyncio.Lock alone
would not coordinate them.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import time
from contextlib import contextmanager

from .config import PROJECT_ROOT
from .settings import settings

LOCK_FILE = PROJECT_ROOT / "state" / "browser.lock"

# Waiting this long for the browser means something upstream is wedged or
# saturated — push an alert (rate-limited) so contention is VISIBLE while it
# happens, not discovered days later in the journal. On 03-07-2026 eight
# consecutive mail-triggered applies each waited out the full 1800s timeout
# (9+ hours of lost prime listings) with zero signal to the user.
WAIT_ALERT_SECONDS = settings().browser_lock_wait_alert_seconds


def holder_info() -> str:
    """Who wrote the lock file last (pid/component/epoch) — diagnostics only."""
    try:
        return LOCK_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return "(unknown)"


def _alert_long_wait(holder: str, waited: float) -> None:
    try:
        # Local import: notify pulls in the Gmail stack; the lock must stay
        # importable (and cheap) everywhere, including in tests without it.
        from .notify import send_alert_dedup
        send_alert_dedup(
            "browser_lock_wait",
            "⏳ Stekkies bot: shared browser contended",
            f"'{holder or 'unknown'}' has been waiting {waited:.0f}s for the "
            f"shared-browser lock.\nCurrent holder: {holder_info()}\n"
            "If this persists, a run is wedged holding the lock — check the "
            "orchestrator journal.",
        )
    except Exception as e:  # noqa: BLE001 - alerting must never break locking
        print(f"[lock] wait alert failed: {e}")


@contextmanager
def browser_lock(timeout: float = 1800.0, poll: float = 0.5, holder: str = ""):
    """Blocking, cross-process exclusive lock. Raises TimeoutError if it cannot
    be acquired within ``timeout`` seconds.

    ``holder`` names the component acquiring it (e.g. "apply:huurwoningen.nl",
    "healthcheck", "self-improvement") — written into the lock file so that
    when someone is stuck WAITING for this lock, the alert/journal can say who
    is actually HOLDING it instead of just "pid 12345".
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    wait_alerted = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                waited = time.monotonic() - started
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire browser lock within {timeout:.0f}s "
                        f"(holder: {holder_info()})") from None
                if not wait_alerted and waited >= WAIT_ALERT_SECONDS:
                    wait_alerted = True
                    _alert_long_wait(holder, waited)
                time.sleep(poll)
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()} holder={holder or '?'} "
                     f"t={time.time():.0f}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


async def acquire_and_run(func, *args, timeout: float = 1800.0,  # noqa: ASYNC109 — timeout feeds the blocking flock in the worker thread; an asyncio-level timeout could not release it (see the MCP-teardown lesson)
                          holder: str = "", **kwargs):
    """Run a BLOCKING callable while holding the browser lock, off the event
    loop (so the watcher's other async polls keep running)."""
    def _locked():
        with browser_lock(timeout=timeout, holder=holder):
            return func(*args, **kwargs)
    return await asyncio.to_thread(_locked)
