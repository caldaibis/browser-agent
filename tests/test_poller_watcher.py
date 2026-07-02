from __future__ import annotations

import asyncio
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.poller import dedup, watcher
from src.poller.models import RawListing


class _FakeResult:
    def __init__(self, outcome: str, terminal: bool, rc: int = 1, summary: str = "",
                 resolved_url: str = ""):
        self.outcome = outcome
        self.rc = rc
        self.summary = summary
        self.terminal = terminal
        self.resolved_url = resolved_url


@contextlib.contextmanager
def _worker_env(td: str, apply_mock):
    patches = [
        patch.object(dedup, "SEEN_FILE", Path(td) / "seen.jsonl"),
        patch.object(dedup, "PROCESSED_FILE", Path(td) / "processed.jsonl"),
        patch.object(dedup, "CLAIMS_FILE", Path(td) / "claims.jsonl"),
        patch.object(dedup, "LOCK_FILE", Path(td) / "dedup.lock"),
        patch.object(watcher, "PROCESSED_FILE", Path(td) / "processed.jsonl"),
        patch.object(watcher, "priority_pending", lambda: False),
        patch.object(watcher, "_summary",
                     return_value={"status": "incomplete", "message": "x"}),
        patch.object(watcher, "_activity"),
        patch.object(watcher, "_log"),
        patch.object(watcher, "send_status_email"),
        patch("src.apply.apply", apply_mock),
        patch("src.self_improvement_agent.improve_after_apply"),
        patch("src.self_improvement_agent.improve_exception"),
    ]
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


_LISTING = dict(
    source_url="https://www.huurwoningen.nl/huren/utrecht/0555f33c/hof-van-oslo/",
    source_name="huurwoningen.nl",
    address="Hof van Oslo",
)


async def _run_worker_until_drained(seen, queue):
    task = asyncio.create_task(watcher._apply_worker(queue, seen))
    try:
        await queue.join()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class TestApplyWorkerNoRetries(unittest.IsolatedAsyncioTestCase):
    async def test_single_non_terminal_attempt_consumes_the_listing(self):
        """One attempt per listing: a non-terminal outcome (incomplete) must
        mark the listing seen + processed immediately — NOT release it for a
        retry. (Hof van Oslo, 01-07-2026: 15+ identical retried runs at ~5M
        tokens each before retries were removed.)"""
        calls = []

        def fake_apply(listing, **kw):
            calls.append(kw)
            return _FakeResult("incomplete", terminal=False)

        with tempfile.TemporaryDirectory() as td, _worker_env(td, fake_apply):
            seen = dedup.SeenStore()
            listing = RawListing(**_LISTING)
            self.assertTrue(seen.reserve(listing.source_url))
            queue: "asyncio.Queue[RawListing]" = asyncio.Queue()
            await queue.put(listing)
            await _run_worker_until_drained(seen, queue)

            self.assertEqual(len(calls), 1)
            # The poller's applies must run with priority yielding enabled.
            self.assertTrue(calls[0].get("yield_to_priority"))
            self.assertFalse(seen.is_new(listing.source_url))
            self.assertEqual(dedup.release_count(listing.source_url), 0)
            processed = (Path(td) / "processed.jsonl").read_text(encoding="utf-8")
            self.assertIn('"incomplete"', processed)

    async def test_yielded_outcome_requeues_without_consuming_an_attempt(self):
        """A run aborted to hand the browser to a priority mail apply is not a
        verdict on the listing: it must be requeued and applied again, and only
        the second (real) outcome recorded."""
        results = [_FakeResult("yielded", terminal=False, rc=125),
                   _FakeResult("submitted", terminal=True, rc=0)]
        calls = []

        def fake_apply(listing, **kw):
            calls.append(listing)
            return results[len(calls) - 1]

        with tempfile.TemporaryDirectory() as td, _worker_env(td, fake_apply):
            seen = dedup.SeenStore()
            listing = RawListing(**_LISTING)
            self.assertTrue(seen.reserve(listing.source_url))
            queue: "asyncio.Queue[RawListing]" = asyncio.Queue()
            await queue.put(listing)
            await _run_worker_until_drained(seen, queue)

            self.assertEqual(len(calls), 2)
            self.assertFalse(seen.is_new(listing.source_url))
            processed = (Path(td) / "processed.jsonl").read_text(encoding="utf-8")
            self.assertIn('"submitted"', processed)
            self.assertNotIn('"yielded"', processed)

    async def test_exception_during_apply_consumes_the_listing(self):
        """A crash mid-apply may already have spent real agent turns — same
        one-attempt rule: recorded as error, never retried."""
        def fake_apply(listing, **kw):
            raise RuntimeError("browser exploded")

        with tempfile.TemporaryDirectory() as td, _worker_env(td, fake_apply):
            seen = dedup.SeenStore()
            listing = RawListing(**_LISTING)
            self.assertTrue(seen.reserve(listing.source_url))
            queue: "asyncio.Queue[RawListing]" = asyncio.Queue()
            await queue.put(listing)
            await _run_worker_until_drained(seen, queue)

            self.assertFalse(seen.is_new(listing.source_url))
            processed = (Path(td) / "processed.jsonl").read_text(encoding="utf-8")
            self.assertIn('"error"', processed)


if __name__ == "__main__":
    unittest.main()
