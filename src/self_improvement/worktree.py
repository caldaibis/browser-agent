"""Throwaway git worktree per self-improvement run.

Always a sibling of the real checkout, branched off freshly-fetched
origin/main, with the main .venv symlinked in (uv follows it; see the
verify-gate notes in AGENTS.md). Removal must never raise."""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import PROJECT_ROOT

# Sibling directory (never nested inside PROJECT_ROOT) holding one throwaway
# worktree per self-improvement run.
WORKTREE_BASE = PROJECT_ROOT.parent / f"{PROJECT_ROOT.name}-self-improvement-worktrees"


def _create_worktree() -> tuple[Path, str]:
    WORKTREE_BASE.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "worktree", "prune"], cwd=PROJECT_ROOT,
                   capture_output=True, text=True, timeout=30)
    subprocess.run(["git", "fetch", "origin", "main"], cwd=PROJECT_ROOT,
                   capture_output=True, text=True, timeout=60)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = WORKTREE_BASE / ts
    branch = f"self-improvement/{ts}"
    r = subprocess.run(
        ["git", "worktree", "add", str(path), "-b", branch, "origin/main"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {r.stdout}{r.stderr}")
    # Symlink (not copy) the main checkout's already-synced .venv so
    # run_verification's `uv run` calls find a fully-installed environment
    # with no sync at all -- uv follows the symlink transparently and the
    # worktree's pyproject.toml/uv.lock are byte-identical at checkout time.
    # Verified empirically (including the full `just check` pipeline).
    # Removing the worktree later only deletes this symlink, never the real
    # venv it points at.
    main_venv = PROJECT_ROOT / ".venv"
    if main_venv.exists():
        (path / ".venv").symlink_to(main_venv)
    return path, branch


def _remove_worktree(path: Path, branch: str, logger: Any) -> None:
    try:
        r = subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                           cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            logger.line(f"[self-improvement] worktree remove failed: {r.stdout}{r.stderr}")
        subprocess.run(["git", "branch", "-D", branch], cwd=PROJECT_ROOT,
                       capture_output=True, text=True, timeout=10)
    except Exception as e:  # noqa: BLE001 - cleanup must never raise
        logger.line(f"[self-improvement] worktree cleanup error: {type(e).__name__}: {e}")


def remove_orphaned_worktrees() -> list[str]:
    """Remove leftovers while the dedicated worker holds its global lock."""
    removed: list[str] = []
    try:
        listing = subprocess.run(
            ["git", "worktree", "list", "--porcelain"], cwd=PROJECT_ROOT,
            capture_output=True, text=True, timeout=30,
        ).stdout
        blocks = listing.split("\n\n")
        base = WORKTREE_BASE.resolve()
        for block in blocks:
            fields = dict(
                line.split(" ", 1) for line in block.splitlines()
                if " " in line
            )
            raw_path = fields.get("worktree")
            branch_ref = fields.get("branch", "")
            if not raw_path:
                continue
            path = Path(raw_path)
            try:
                managed = path.resolve().is_relative_to(base)
            except OSError:
                managed = False
            if not managed:
                continue
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30,
            )
            branch = branch_ref.removeprefix("refs/heads/")
            if branch:
                subprocess.run(
                    ["git", "branch", "-D", branch], cwd=PROJECT_ROOT,
                    capture_output=True, text=True, timeout=10,
                )
            removed.append(str(path))
        subprocess.run(["git", "worktree", "prune"], cwd=PROJECT_ROOT,
                       capture_output=True, text=True, timeout=30)
    except Exception:
        return removed
    return removed
