"""Self-improvement layer, made visible.

The dashboard previously showed nothing about the loop that repairs apply
failures — even though that loop's health (is it landing fixes? is it burning
tokens re-diagnosing the same incident? are there unlanded patches rotting on
disk?) is exactly what the maintainer needs to steer it. This module
aggregates the SI layer's existing on-disk state for `/self-improvement`:

- runs        logs/self_improvement.jsonl (+ per-run logs/self_improvement/*.log for cost)
- incidents   state/self_improvement/incidents.jsonl (via incident_store)
- gates       state/known_gates.json (via known_gates)
- patches     state/pending_patches/*.patch
- guards      logs/trajectories/*.jsonl (via digest._guard_stats)
- playbooks   state/site_playbooks/*.md
- lineage     state/self_improvement/lineage.jsonl

All reads fail open; several of these files may not exist yet.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

from ..config import LOG_DIR, PROJECT_ROOT
from . import cache, costs
from .data import parse_ts

SI_RUN_LOG = LOG_DIR / "self_improvement.jsonl"
SI_LOG_DIR = LOG_DIR / "self_improvement"
PENDING_PATCH_DIR = PROJECT_ROOT / "state" / "pending_patches"
PLAYBOOK_DIR = PROJECT_ROOT / "state" / "site_playbooks"
LINEAGE_LOG = PROJECT_ROOT / "state" / "self_improvement" / "lineage.jsonl"

_FAILURE_ACTIONS = {"error", "fix_failed", "timeout", "incomplete"}
_PATCH_SUBJECT_RE = re.compile(r"^Subject:\s*(?:\[PATCH[^\]]*\]\s*)?(.+)$", re.MULTILINE)


def _si_log_by_ts() -> list[tuple[datetime, Path]]:
    out: list[tuple[datetime, Path]] = []
    try:
        for p in SI_LOG_DIR.glob("*.log"):
            try:
                out.append((datetime.strptime(p.stem[:15], "%Y%m%d_%H%M%S"), p))
            except ValueError:
                continue
    except OSError:
        return []
    return sorted(out)


def _match_log(rec: dict, logs: list[tuple[datetime, Path]]) -> Path | None:
    # Prefer the explicit log_path recorded on the run; fall back to the
    # per-run log whose timestamp is closest to the record's ts.
    explicit = rec.get("log_path")
    if explicit and Path(explicit).exists():
        return Path(explicit)
    ts = parse_ts(rec.get("ts"))
    if ts is None or not logs:
        return None
    return min(logs, key=lambda pt: abs((pt[0] - ts).total_seconds()))[1]


def runs(limit: int = 100) -> list[dict]:
    """Recent self-improvement runs, newest first, with attached cost."""
    logs = _si_log_by_ts()
    out: list[dict] = []
    for rec in cache.jsonl_records(SI_RUN_LOG):
        event = rec.get("event")
        if event not in ("done", "error", "skipped_duplicate_incident"):
            continue
        log_path = _match_log(rec, logs) if event != "skipped_duplicate_incident" else None
        out.append({
            "ts": rec.get("ts", ""),
            "event": event,
            "trigger_outcome": rec.get("status", ""),
            "action": rec.get("action") or (event if event != "done" else "unknown"),
            "deployed": bool(rec.get("deployed")),
            "code_changed": bool(rec.get("code_changed")),
            "root_cause": rec.get("root_cause") or rec.get("reason") or "",
            "summary": rec.get("summary") or rec.get("error") or "",
            "fingerprint": rec.get("fingerprint", ""),
            "cost_usd": costs.si_log_cost(log_path) if log_path else None,
            "log_name": log_path.stem if log_path else "",
            "failed": (event == "error") or (str(rec.get("action") or "") in _FAILURE_ACTIONS),
        })
    out.reverse()
    return out[:limit]


def kpis(days: int = 7) -> dict:
    cutoff = datetime.now() - timedelta(days=days)
    runs_window = [r for r in runs(limit=10_000)
                   if (parse_ts(r["ts"]) or datetime.min) >= cutoff]
    real = [r for r in runs_window if r["event"] != "skipped_duplicate_incident"]
    deployed = sum(1 for r in real if r["deployed"])
    fix_attempts = sum(1 for r in real if r["action"] in ("fixed_deployed", "fix_failed"))
    skipped = sum(1 for r in runs_window if r["event"] == "skipped_duplicate_incident")
    spend = sum(r["cost_usd"] or 0.0 for r in real)
    return {
        "days": days,
        "runs": len(real),
        "deployed": deployed,
        "landing_rate": round(100 * deployed / fix_attempts, 1) if fix_attempts else None,
        "skipped_duplicates": skipped,
        "spend_usd": round(spend, 4),
    }


def pending_patches() -> list[dict]:
    out: list[dict] = []
    try:
        paths = sorted(PENDING_PATCH_DIR.glob("*.patch"), reverse=True)
    except OSError:
        return out
    for p in paths:
        subject = ""
        try:
            m = _PATCH_SUBJECT_RE.search(p.read_text(encoding="utf-8", errors="replace")[:4000])
            if m:
                subject = m.group(1).strip()
        except OSError:
            pass
        out.append({
            "name": p.name,
            "subject": subject or p.stem,
            "age": _age(p),
            "git_am": f"git am state/pending_patches/{p.name}",
        })
    return out


def patch_content(name: str) -> str | None:
    """Redacted patch text. `name` must be a bare *.patch filename."""
    if not re.fullmatch(r"[\w.\-]+\.patch", name or ""):
        return None
    path = (PENDING_PATCH_DIR / name).resolve()
    if path.parent != PENDING_PATCH_DIR.resolve() or not path.exists():
        return None
    from .data import redact
    try:
        return redact(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def run_log(name: str) -> str | None:
    """Redacted per-run SI log. `name` is the stem (no path, no extension)."""
    if not re.fullmatch(r"[0-9_]+", name or ""):
        return None
    path = (SI_LOG_DIR / f"{name}.log").resolve()
    if path.parent != SI_LOG_DIR.resolve() or not path.exists():
        return None
    from .data import redact
    try:
        return redact(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def incidents(days: int = 30) -> list[dict]:
    from ..incident_store import incident_summary
    try:
        return incident_summary(days=days)
    except Exception:
        return []


def gates() -> list[dict]:
    """Active gates + expired ones (flagged) so the operator sees both."""
    from ..known_gates import GATES_PATH, load_gates
    active = {(g.get("domain"), g.get("kind")) for g in load_gates()}
    out: list[dict] = []
    try:
        import json
        raw = json.loads(GATES_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = []
    for g in raw if isinstance(raw, list) else []:
        if not isinstance(g, dict):
            continue
        out.append({**g, "expired": (g.get("domain"), g.get("kind")) not in active})
    out.sort(key=lambda g: (g["expired"], str(g.get("domain"))))
    return out


def guard_trend(days: int = 7) -> dict[str, int]:
    from ..digest import _guard_stats
    try:
        return _guard_stats(datetime.now() - timedelta(days=days))
    except Exception:
        return {}


def playbooks() -> list[dict]:
    out: list[dict] = []
    try:
        paths = sorted(PLAYBOOK_DIR.glob("*.md"))
    except OSError:
        return out
    for p in paths:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        out.append({"domain": p.stem, "chars": size, "age": _age(p)})
    return out


def playbook_content(domain: str) -> str | None:
    from ..known_gates import normalize_domain
    dom = normalize_domain(domain)
    if not dom:
        return None
    path = (PLAYBOOK_DIR / f"{dom}.md").resolve()
    if path.parent != PLAYBOOK_DIR.resolve() or not path.exists():
        return None
    from .data import redact
    try:
        return redact(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def lineage() -> dict:
    last_eval = last_mine = None
    for rec in cache.jsonl_records(LINEAGE_LOG):
        if rec.get("event") == "eval":
            last_eval = rec
        elif rec.get("event") == "mine":
            last_mine = rec
    return {"eval": last_eval, "mine": last_mine}


def _age(path: Path) -> str:
    from .data import format_age
    try:
        secs = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
    except OSError:
        return "unknown"
    return format_age(secs)
