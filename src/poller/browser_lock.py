"""Exclusive lock on the shared CDP browser.

Only ONE component may drive the browser via the Playwright MCP at a time. The
applier (apply.py) holds this for a whole submission; tier-3 watchers and the
Stekkies orchestrator must acquire it before touching the browser and yield
while it is held.

Backed by an OS file lock (fcntl.flock) so it is exclusive ACROSS PROCESSES —
the poller and the Stekkies orchestrator are separate systemd services sharing
one browser, and an in-process asyncio.Lock alone would not coordinate them.
"""
from __future__ import annotations

import asyncio
import fcntl
import os
import time
from contextlib import contextmanager

from ..config import PROJECT_ROOT

LOCK_FILE = PROJECT_ROOT / "state" / "browser.lock"


@contextmanager
def browser_lock(timeout: float = 1800.0, poll: float = 0.5):
    """Blocking, cross-process exclusive lock. Raises TimeoutError if it cannot
    be acquired within ``timeout`` seconds."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire browser lock within {timeout:.0f}s")
                time.sleep(poll)
        os.write(fd, f"pid={os.getpid()} t={time.time():.0f}\n".encode())
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


async def acquire_and_run(func, *args, timeout: float = 1800.0, **kwargs):
    """Run a BLOCKING callable while holding the browser lock, off the event
    loop (so the watcher's other async polls keep running)."""
    def _locked():
        with browser_lock(timeout=timeout):
            return func(*args, **kwargs)
    return await asyncio.to_thread(_locked)
