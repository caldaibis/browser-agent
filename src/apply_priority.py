"""Mail-apply priority over the poller.

Dutch rentals are won by minutes. A mail-triggered apply (Stekkies /
Huurwoningen alert — someone else was just notified too) is high-intent and
time-critical; a poller-triggered apply is speculative discovery. Both drive
the one shared browser behind an exclusive flock, so without coordination a
mail apply can queue behind a poller run holding the lock for up to
APPLY_TIMEOUT_SECONDS (15 min) — exactly the window that decides who gets the
viewing.

The orchestrator claims a priority flag (a state/ file) for the whole handling
of a mail listing — extraction included, since extraction drives the browser
too. The poller honors it in two places:
  - `poller.watcher._apply_worker` waits for the flag to clear before starting
    a new apply, and
  - an in-flight poller run checks it once per agent turn
    (`browser_agent._run`'s ``yield_check``) and aborts with outcome
    ``yielded``; the watcher requeues the listing untouched, so the browser
    lock frees within one turn (~seconds) instead of after the full run.

A crashed orchestrator cannot wedge the poller: a flag older than
APPLY_PRIORITY_STALE_SECONDS is ignored, and the next claim overwrites it.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

from .config import PROJECT_ROOT
from .settings import settings

PRIORITY_FLAG = PROJECT_ROOT / "state" / "apply_priority.flag"

# A mail apply is extraction + one agent run (APPLY_TIMEOUT_SECONDS=900 by
# default), so a flag this old means the claimant died without cleanup.
STALE_SECONDS = settings().apply_priority_stale_seconds


@contextmanager
def priority_claim():
    """Hold the mail-apply priority flag for the duration of the block."""
    PRIORITY_FLAG.parent.mkdir(parents=True, exist_ok=True)
    PRIORITY_FLAG.write_text(
        f"pid={os.getpid()} epoch={time.time():.0f}\n", encoding="utf-8")
    try:
        yield
    finally:
        try:
            PRIORITY_FLAG.unlink(missing_ok=True)
        except OSError:
            pass


def priority_pending() -> bool:
    """True while a (fresh) mail-apply priority claim is held."""
    try:
        st = PRIORITY_FLAG.stat()
    except (FileNotFoundError, OSError):
        return False
    return (time.time() - st.st_mtime) <= STALE_SECONDS
