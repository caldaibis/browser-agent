"""Drain durable self-improvement jobs serially (systemd oneshot worker)."""
from __future__ import annotations

import traceback

from . import self_improvement_queue as queue


def process_job(job: dict) -> None:
    from .self_improvement_agent import process_queued_job

    process_queued_job(job)


def main() -> int:
    with queue.worker_lock() as acquired:
        if not acquired:
            print("[self-improvement-worker] another worker is active; exiting")
            return 0
        recovered = queue.recover_orphans()
        if recovered:
            print(f"[self-improvement-worker] recovered {recovered} orphaned job(s)")
        from .self_improvement_agent import record_abandoned_runs
        abandoned = record_abandoned_runs()
        if abandoned:
            print(f"[self-improvement-worker] closed {len(abandoned)} abandoned run record(s)")
        from .self_improvement.worktree import remove_orphaned_worktrees
        removed = remove_orphaned_worktrees()
        if removed:
            print(f"[self-improvement-worker] removed {len(removed)} orphaned worktree(s)")
        while claimed := queue.claim_next():
            path, job = claimed
            job_id = str(job.get("job_id") or path.stem)
            print(f"[self-improvement-worker] processing {job_id}", flush=True)
            try:
                process_job(job)
            except Exception as exc:  # noqa: BLE001 - preserve poison jobs
                detail = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                queue.fail(path, detail)
                print(f"[self-improvement-worker] failed {job_id}: {exc}", flush=True)
            else:
                queue.complete(path)
                print(f"[self-improvement-worker] completed {job_id}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
