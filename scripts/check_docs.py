#!/usr/bin/env python3
"""Validate local Markdown link targets without network access.

Repository documentation uses relative Markdown links as its navigation
contract. This catches the common agent-edit failure where a file is moved or
renamed but guides continue pointing at the old location.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "logs",
    "state",
}
LINK_RE = re.compile(r"\[[^]]*\]\(([^)]+)\)")
EXTERNAL_SCHEMES = {"http", "https", "mailto", "tel", "data"}


def markdown_files() -> list[Path]:
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts)
    )


def link_target(raw: str) -> str:
    """Return the destination without an optional Markdown title."""
    value = raw.strip()
    if value.startswith("<") and ">" in value:
        return value[1 : value.index(">")]
    return value.split(maxsplit=1)[0]


def main() -> int:
    failures: list[str] = []
    checked = 0
    files = markdown_files()

    for source in files:
        for lineno, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            for match in LINK_RE.finditer(line):
                raw = link_target(match.group(1))
                parsed = urlsplit(raw)
                if not raw or raw.startswith("#") or parsed.scheme in EXTERNAL_SCHEMES:
                    continue

                path_text = unquote(parsed.path)
                if not path_text:
                    continue
                target = (ROOT / path_text.lstrip("/")) if path_text.startswith("/") else (source.parent / path_text)
                checked += 1
                if not target.resolve().exists():
                    rel_source = source.relative_to(ROOT)
                    failures.append(f"{rel_source}:{lineno}: missing target {raw!r}")

    if failures:
        print("documentation link check failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        return 1

    print(f"docs ok: {len(files)} Markdown files, {checked} local links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
