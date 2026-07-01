from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.poller import dedup, watcher
from src.poller.models import RawListing


class _FakeResult:
    def __init__(self, outcome: str, terminal: bool, rc: int = 1, summary: str = ""):
        self.outcome = outcome
        self.rc = rc
        self.summary = summary
        self.terminal = terminal


class TestApplyWorkerRetryCap(unittest.IsolatedAsyncioTestCase):
    async def test_gives_up_after_max_attempts_on_repeated_non_terminal_outcome(self):
        with tempfile.TemporaryDirectory() as td:
            with (
                patch.object(dedup, "SEEN_FILE", Path(td) / "seen.jsonl"),
                patch.object(dedup, "PROCESSED_FILE", Path(td) / "processed.jsonl"),
                patch.object(dedup, "CLAIMS_FILE", Path(td) / "claims.jsonl"),
                patch.object(dedup, "LOCK_FILE", Path(td) / "dedup.lock"),
                patch.object(watcher, "PROCESSED_FILE", Path(td) / "processed.jsonl"),
                patch.object(watcher, "MAX_POLLER_ATTEMPTS", 2),
                patch.object(watcher, "_summary", return_value={"status": "incomplete", "message": "x"}),
                patch.object(watcher, "_activity"),
                patch.object(watcher, "_log"),
                patch.object(watcher, "send_status_email"),
                patch("src.apply.apply", return_value=_FakeResult("incomplete", terminal=False)),
                patch("src.self_improvement_agent.improve_after_apply"),
            ):
                seen = dedup.SeenStore()
                listing = RawListing(
                    source_url="https://www.huurwoningen.nl/huren/utrecht/0555f33c/hof-van-oslo/",
                    source_name="huurwoningen.nl",
                    address="Hof van Oslo",
                )

                queue: "asyncio.Queue[RawListing]" = asyncio.Queue()
                task = asyncio.create_task(watcher._apply_worker(queue, seen))
                try:
                    for _ in range(2):
                        self.assertTrue(seen.reserve(listing.source_url))
                        await queue.put(listing)
                        await queue.join()
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

                # Two non-terminal attempts (the MAX_POLLER_ATTEMPTS cap) should
                # have given up on the listing instead of leaving it retryable.
                self.assertEqual(dedup.release_count(listing.source_url), 1)
                self.assertFalse(seen.is_new(listing.source_url))


if __name__ == "__main__":
    unittest.main()
