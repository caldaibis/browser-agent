"""Structured failure evidence for the self-improvement layer.

Three pieces, all offline and fail-open:
- `record_trajectory_event` — called from the hot apply loop
  (`browser_agent._run`) to append redacted, typed JSONL events (turn usage,
  tool calls/results, guard firings, final outcome) per run. This is the
  machine-readable counterpart of the free-text transcript.
- `classify_failure` — deterministic first-pass weakness classifier used for
  incident fingerprinting (`src/incident_store.py`), failure mining, and the
  eval fixtures.
- `mine_failures` / `eval_harness` — CLI (`just self-improve-mine` /
  `self-improve-eval`): cluster recent failed transcripts into evidence
  bundles, and run the fixture regressions that keep AGENTS.md's hard-won
  lessons executable (the eval also runs inside `just check` via
  tests/test_self_improvement_harness.py).

Deliberately NOT here: autonomous "harness evolution" (self-patching driven
from mined proposals). That path duplicated src/self_improvement_agent.py
with weaker guardrails and was dropped; code changes go through the guarded
agent in self_improvement_agent.py only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import LOG_DIR, PROJECT_ROOT


STATE_DIR = PROJECT_ROOT / "state" / "self_improvement"
EVIDENCE_DIR = STATE_DIR / "evidence"
LINEAGE_LOG = STATE_DIR / "lineage.jsonl"
TRAJECTORY_DIR = LOG_DIR / "trajectories"
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "self_improvement_harness"

SELF_IMPROVEMENT_EVAL_FIXTURES = os.environ.get(
    "SELF_IMPROVEMENT_EVAL_FIXTURES", str(DEFAULT_FIXTURE_DIR))

HARNESS_SURFACES = {
    "prompt_context",
    "tool_registry",
    "memory",
    "control_policy",
    "observability",
    "evaluator",
}

# Keys whose VALUES are secrets and must be redacted. NB: a bare "token" was
# deliberately dropped — it matched the per-turn telemetry counters
# (`prompt_tokens`, `completion_tokens`, `cache_hit_tokens`, …), redacting the
# token *counts* to "***" and destroying the trajectory cost/timeline data
# (and crashing the dashboard's int() parse). Real auth secrets are matched by
# their compound names instead.
SECRET_KEY_RE = re.compile(
    r"(password|passwd|secret|api[_-]?key|authorization|cookie"
    r"|access[_-]?token|refresh[_-]?token|auth[_-]?token|bearer)",
    re.IGNORECASE,
)
OUTCOME_RE = re.compile(r"OUTCOME:\s*([a-z_]+)", re.IGNORECASE)
URL_HOST_RE = re.compile(r"https?://([^/\s)]+)", re.IGNORECASE)


@dataclass
class FailureSignature:
    signature: str
    surface: str
    outcome: str
    domain: str
    reason: str


@dataclass
class EvidenceCluster:
    signature: str
    surface: str
    outcome: str
    count: int
    domains: list[str]
    examples: list[dict[str, Any]]


@dataclass
class EvalCaseResult:
    name: str
    passed: bool
    expected_surface: str
    actual_surface: str
    expected_signature: str
    actual_signature: str


def redact_value(value: Any, *, max_string: int = 4000) -> Any:
    """Redact secrets recursively before data hits trajectory/evidence files."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                out[key] = "***"
            else:
                out[key] = redact_value(item, max_string=max_string)
        return out
    if isinstance(value, list):
        return [redact_value(item, max_string=max_string) for item in value]
    if isinstance(value, tuple):
        return [redact_value(item, max_string=max_string) for item in value]
    if isinstance(value, str):
        text = _dashboard_redact(value)
        if len(text) > max_string:
            return text[:max_string] + f"\n[truncated at {max_string} chars]"
        return text
    return value


def record_trajectory_event(run_id: str, event: str, payload: dict[str, Any] | None = None) -> None:
    """Append one redacted JSONL trajectory event.

    This is called from the hot apply loop, so every failure is swallowed.
    """
    if os.environ.get("APPLY_TRAJECTORY_ENABLED", "1") == "0":
        return
    try:
        safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_id or "run")[:120]
        TRAJECTORY_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "run_id": safe_run_id,
            "event": event,
            "payload": redact_value(payload or {}),
        }
        with (TRAJECTORY_DIR / f"{safe_run_id}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        return


def classify_failure(text: str, *, outcome: str = "", domain: str = "") -> FailureSignature:
    """Deterministic first-pass weakness classifier.

    LLM clustering can be layered on top later; this gives stable signatures
    for tests, eval fixtures, incident fingerprints, and evidence bundles.
    """
    low = (text or "").lower()
    outcome = (outcome or _extract_outcome(text) or "unknown").lower()
    domain = domain or _extract_domain(text)

    if any(x in low for x in ("mollie", "stripe", "adyen", "paypal", "checkout", "payment page")):
        return FailureSignature(
            "payment-checkout-hard-stop", "control_policy", outcome, domain,
            "Payment or checkout flow reached or discussed.",
        )
    if "already has a recorded submission" in low or ("duplicate" in low and "destination" in low):
        return FailureSignature(
            "cross-source-dedup", "control_policy", outcome, domain,
            "Run needed cross-source duplicate prevention.",
        )
    if any(x in low for x in ("dom_scan", "click_by_text", "fill_by_label", "select_option_by_label")):
        return FailureSignature(
            "refless-dialog-dom-fallback", "tool_registry", outcome, domain,
            "Accessibility snapshot missed controls that raw DOM tools can reach.",
        )
    if any(x in low for x in ("dialog", "aria", "0x0", "duplicate id", "hidden duplicate")):
        return FailureSignature(
            "inaccessible-dialog-controls", "tool_registry", outcome, domain,
            "Page likely used inaccessible or duplicate-id dialog controls.",
        )
    if any(x in low for x in ("browser lock", "browser_lock", "flock", "cdp connect", "lock held")):
        return FailureSignature(
            "browser-lock-contention", "control_policy", outcome, domain,
            "Run blocked on the shared-browser lock or a hung CDP connection.",
        )
    if any(x in low for x in ("snapshot-overuse", "stale page dump", "pruned", "quadratic")):
        return FailureSignature(
            "context-growth-snapshot-loop", "control_policy", outcome, domain,
            "Snapshots or page dumps dominated context/turn budget.",
        )
    if any(x in low for x in ("finish=length", "truncated/dropped", "empty content", "without a valid outcome")):
        return FailureSignature(
            "empty-or-truncated-model-turn", "control_policy", outcome, domain,
            "Model/API turn ended without usable tool call or outcome.",
        )
    if any(x in low for x in ("login_required", "stored password", "credential", "wachtwoord", "2fa")):
        return FailureSignature(
            "login-account-state", "memory", outcome, domain,
            "Run blocked on credential or account state.",
        )
    if any(x in low for x in ("not_eligible", "inkomenseis", "studenten", "woningdelers")):
        return FailureSignature(
            "eligibility-gate", "prompt_context", outcome, domain,
            "Eligibility decision should happen before form work.",
        )
    if any(x in low for x in ("no source url", "no_source_url", "redirect")):
        return FailureSignature(
            "source-url-extraction", "prompt_context", outcome, domain,
            "Input/extraction did not provide an actionable source URL.",
        )
    return FailureSignature(
        "unclassified-apply-failure", "observability", outcome, domain,
        "No deterministic weakness pattern matched; improve trace evidence.",
    )


def mine_failures(
    *,
    transcript_dir: Path | None = None,
    output_dir: Path | None = None,
    max_files: int = 200,
) -> dict[str, Any]:
    transcript_dir = transcript_dir or (LOG_DIR / "transcripts")
    output_dir = output_dir or EVIDENCE_DIR
    records: list[dict[str, Any]] = []
    paths = sorted(transcript_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:max_files]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        outcome = _extract_outcome(text) or _outcome_from_text(text)
        if outcome in {"submitted", "already_applied", "not_eligible", "payment_required"}:
            continue
        sig = classify_failure(text[-80000:], outcome=outcome)
        records.append({
            "path": str(path),
            "outcome": sig.outcome,
            "signature": sig.signature,
            "surface": sig.surface,
            "domain": sig.domain,
            "reason": sig.reason,
            "excerpt": redact_value(text[-1200:], max_string=1200),
        })

    clusters = _cluster_records(records)
    bundle = {
        "schema": "self-improvement-evidence-v1",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source": str(transcript_dir),
        "record_count": len(records),
        "clusters": [asdict(c) for c in clusters],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_lineage("mine", {"bundle": str(out_path), "records": len(records)})
    return bundle


def eval_harness(fixture_dir: Path | None = None) -> dict[str, Any]:
    fixture_dir = fixture_dir or Path(SELF_IMPROVEMENT_EVAL_FIXTURES)
    results: list[EvalCaseResult] = []
    if not fixture_dir.exists():
        return {
            "schema": "self-improvement-eval-v1",
            "fixture_dir": str(fixture_dir),
            "passed": 0,
            "failed": 0,
            "results": [],
        }
    for path in sorted(fixture_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        sig = classify_failure(
            str(data.get("transcript") or ""),
            outcome=str(data.get("outcome") or ""),
            domain=str(data.get("domain") or ""),
        )
        expected_surface = str(data.get("expected_surface") or "")
        expected_signature = str(data.get("expected_signature") or "")
        passed = (
            sig.surface == expected_surface
            and (not expected_signature or expected_signature in sig.signature)
        )
        results.append(EvalCaseResult(
            name=path.stem,
            passed=passed,
            expected_surface=expected_surface,
            actual_surface=sig.surface,
            expected_signature=expected_signature,
            actual_signature=sig.signature,
        ))
    summary = {
        "schema": "self-improvement-eval-v1",
        "fixture_dir": str(fixture_dir),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "results": [asdict(r) for r in results],
    }
    _append_lineage("eval", {"passed": summary["passed"], "failed": summary["failed"]})
    return summary


def _cluster_records(records: list[dict[str, Any]]) -> list[EvidenceCluster]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for rec in records:
        grouped[(rec["signature"], rec["surface"], rec["outcome"])].append(rec)
    clusters = []
    for (signature, surface, outcome), items in grouped.items():
        domain_counts = Counter(str(i.get("domain") or "") for i in items)
        domains = [d for d, _count in domain_counts.most_common(8) if d]
        clusters.append(EvidenceCluster(
            signature=signature,
            surface=surface,
            outcome=outcome,
            count=len(items),
            domains=domains,
            examples=items[:5],
        ))
    clusters.sort(key=lambda c: (-c.count, c.signature))
    return clusters


def _append_lineage(event: str, payload: dict[str, Any]) -> None:
    try:
        LINEAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "event": event,
            "payload": redact_value(payload, max_string=8000),
        }
        with LINEAGE_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        return


def _extract_outcome(text: str) -> str:
    matches = OUTCOME_RE.findall(text or "")
    return matches[-1].lower() if matches else ""


def _outcome_from_text(text: str) -> str:
    low = (text or "").lower()
    for outcome in (
        "timeout", "incomplete", "blocked", "login_required", "not_available",
        "error", "unknown", "no_source_url",
    ):
        if outcome in low:
            return outcome
    return "unknown"


def _extract_domain(text: str) -> str:
    match = URL_HOST_RE.search(text or "")
    if not match:
        return ""
    host = match.group(1).lower().split(":", 1)[0]
    return host[4:] if host.startswith("www.") else host


def _dashboard_redact(text: str) -> str:
    try:
        from .dashboard.data import redact

        return redact(text)
    except Exception:
        return text


def _print_json(data: Any) -> int:
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mine/evaluate self-improvement failure evidence.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    mine_p = sub.add_parser("mine")
    mine_p.add_argument("--transcripts", type=Path, default=LOG_DIR / "transcripts")
    mine_p.add_argument("--max-files", type=int, default=200)
    sub.add_parser("eval")
    args = parser.parse_args(argv)
    if args.cmd == "mine":
        return _print_json(mine_failures(transcript_dir=args.transcripts, max_files=args.max_files))
    if args.cmd == "eval":
        return _print_json(eval_harness())
    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
