from __future__ import annotations

import asyncio
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from src import self_improvement_agent as sia
from src import self_improvement_queue as queue
from src.browser_agent import AgentResult
from src.self_improvement_harness import classify_failure


class QueuePaths:
    def __init__(self, root: Path):
        self.stack = patch.multiple(
            queue,
            QUEUE_ROOT=root,
            PENDING_DIR=root / "pending",
            RUNNING_DIR=root / "running",
            FAILED_DIR=root / "failed",
            RUN_LOCK=root / "worker.lock",
        )

    def __enter__(self):
        return self.stack.__enter__()

    def __exit__(self, *args):
        return self.stack.__exit__(*args)


class TestDurableQueue(unittest.TestCase):
    def test_enqueue_claim_complete_round_trip(self):
        with tempfile.TemporaryDirectory() as td, QueuePaths(Path(td)):
            job_id = queue.enqueue("apply", {"summary": "x"})
            claimed = queue.claim_next()
            self.assertIsNotNone(claimed)
            path, job = claimed
            self.assertEqual(job["job_id"], job_id)
            self.assertEqual(queue.queue_counts()["running"], 1)
            queue.complete(path)
            self.assertEqual(queue.queue_counts()["running"], 0)

    def test_recover_orphaned_running_job(self):
        with tempfile.TemporaryDirectory() as td, QueuePaths(Path(td)):
            queue.enqueue("apply", {"summary": "x"})
            queue.claim_next()
            self.assertEqual(queue.recover_orphans(), 1)
            self.assertEqual(queue.queue_counts()["pending"], 1)

    def test_worker_lock_is_non_reentrant_across_descriptors(self):
        with tempfile.TemporaryDirectory() as td, QueuePaths(Path(td)):
            with queue.worker_lock() as first:
                with queue.worker_lock() as second:
                    self.assertTrue(first)
                    self.assertFalse(second)


class TestEvidenceAndClassification(unittest.TestCase):
    def test_exception_group_keeps_nested_cause(self):
        error = ExceptionGroup("startup", [RuntimeError(
            "Playwright requires Node.js 20 or higher")])
        text = sia._format_exception_evidence(error)
        self.assertIn("Playwright requires Node.js 20", text)
        sig = classify_failure(text, outcome="error", domain="example.test")
        self.assertEqual(sig.signature, "runtime-version-mismatch")
        self.assertEqual(sig.domain, "")

    def test_abandoned_run_is_closed_for_health_metrics(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "self_improvement.jsonl"
            log.write_text(json.dumps({
                "ts": "2026-07-10T10:00:00+00:00",
                "event": "run_started", "run_id": "orphan-1",
            }) + "\n", encoding="utf-8")
            with patch.object(sia, "RUN_LOG", log):
                self.assertEqual(sia.record_abandoned_runs(), ["orphan-1"])
                self.assertEqual(sia.record_abandoned_runs(), [])
            text = log.read_text(encoding="utf-8")
            self.assertIn('"event": "run_abandoned"', text)
            self.assertIn('"action": "orphan_recovered"', text)

    def test_mcp_initialize_failure_is_global(self):
        sig = classify_failure(
            "mcp.shared.exceptions.McpError: Connection closed",
            outcome="error", domain="example.test")
        self.assertEqual(sig.signature, "mcp-initialize-failure")
        self.assertEqual(sig.domain, "")


class TestAuthoritativeToolState(unittest.TestCase):
    def test_commit_results_are_recoverable_without_model_marker(self):
        main = sia._result_from_commit_output(
            "pushed to main; CI/CD deploy triggered; user email sent")
        self.assertEqual(main.action, "fixed_deployed")
        self.assertTrue(main.deployed)
        review = sia._result_from_commit_output(
            "main moved ahead; pushed to self-improvement/x instead; user email sent")
        self.assertEqual(review.action, "fixed_review")
        self.assertTrue(review.code_changed)
        self.assertFalse(review.deployed)


class TestWorktreePolicy(unittest.TestCase):
    def _guard(self, root: Path, phase: str, name: str, data: dict):
        return asyncio.run(sia._make_can_use_tool(root, phase)(name, data, None))

    def test_edit_outside_worktree_is_denied(self):
        with tempfile.TemporaryDirectory() as td:
            result = self._guard(Path(td), "patch", "Edit", {
                "file_path": "/home/deploy/browser-agent/src/apply.py"})
        self.assertEqual(result.behavior, "deny")

    def test_relative_edit_and_read_only_git_are_allowed(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(self._guard(
                root, "patch", "Edit", {"file_path": "src/apply.py"}).behavior,
                "allow")
            self.assertEqual(self._guard(
                root, "patch", "Bash", {"command": "git diff -- src/apply.py"}).behavior,
                "allow")

    def test_git_add_and_live_checkout_bash_are_denied(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self.assertEqual(self._guard(
                root, "patch", "Bash", {"command": "git add src/apply.py"}).behavior,
                "deny")
            self.assertEqual(self._guard(
                root, "patch", "Bash", {
                    "command": f"cd {sia.PROJECT_ROOT} && git status"}).behavior,
                "deny")


class TestQueueFacade(unittest.TestCase):
    def test_apply_failure_is_queued_not_run_inline(self):
        with patch.object(sia.self_improvement_queue, "enqueue", return_value="job-1") as enqueue, \
             patch.object(sia, "_log"):
            result = sia.improve_after_apply(
                listing={"source_url": "https://example.test/x"},
                result=AgentResult(rc=2, outcome="error", summary="boom"),
                trigger="poller",
            )
        self.assertEqual(result.action, "queued")
        enqueue.assert_called_once()

    def test_queue_failure_never_raises_into_apply_pipeline(self):
        with patch.object(sia.self_improvement_queue, "enqueue",
                          side_effect=OSError("disk full")), \
             patch.object(sia, "_log"):
            result = sia.improve_after_apply(
                listing={"source_url": "https://example.test/x"},
                result=AgentResult(rc=2, outcome="error", summary="boom"),
                trigger="poller",
            )
        self.assertEqual(result.action, "error")
        self.assertIn("disk full", result.summary)


if __name__ == "__main__":
    unittest.main()
