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
import re
import fnmatch
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import LOG_DIR, PROJECT_ROOT
from .settings import settings
from .eventlog import utc_now_iso


STATE_DIR = PROJECT_ROOT / "state" / "self_improvement"
EVIDENCE_DIR = STATE_DIR / "evidence"
LINEAGE_LOG = STATE_DIR / "lineage.jsonl"
TRAJECTORY_DIR = LOG_DIR / "trajectories"
DEFAULT_FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "self_improvement_harness"
DEFAULT_APPLY_EVAL_DIR = PROJECT_ROOT / "tests" / "fixtures" / "apply_harness_eval"

SELF_IMPROVEMENT_EVAL_FIXTURES = (
    settings().self_improvement_eval_fixtures or str(DEFAULT_FIXTURE_DIR))
APPLY_HARNESS_EVAL_FIXTURES = (
    settings().apply_harness_eval_fixtures or str(DEFAULT_APPLY_EVAL_DIR))

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


@dataclass
class FailureRecord:
    signature: str
    surface: str
    outcome: str
    domain: str
    terminal_cause: str
    causal_behavior: str
    mechanism: str
    evidence: str


@dataclass
class ApplyEvalCaseResult:
    name: str
    passed: bool
    score: int
    failures: list[str]


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
    if not settings().apply_trajectory_enabled:
        return
    try:
        safe_run_id = re.sub(r"[^A-Za-z0-9_.-]+", "-", run_id or "run")[:120]
        TRAJECTORY_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": utc_now_iso(),
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


def build_failure_record(text: str, *, outcome: str = "", domain: str = "") -> FailureRecord:
    """Verifier-grounded failure record used by mining and proposal prompts.

    This stays deterministic and offline. LLM clustering can consume these
    records later, but the hot path gets stable causal fields today.
    """
    sig = classify_failure(text, outcome=outcome, domain=domain)
    low = (text or "").lower()
    behavior = "unknown agent behavior"
    mechanism = "insufficient trace evidence"
    terminal = sig.reason
    if sig.signature == "payment-checkout-hard-stop":
        behavior = "agent reached or considered a payment/checkout flow"
        mechanism = "control policy must stop before paid registration or checkout"
    elif sig.signature in {"refless-dialog-dom-fallback", "inaccessible-dialog-controls"}:
        behavior = "agent could not operate controls visible in the DOM but absent from snapshots"
        mechanism = "tool registry needs narrow DOM fallbacks and prompt guidance for inaccessible dialogs"
    elif sig.signature == "browser-lock-contention":
        behavior = "agent or diagnostic process waited on the shared browser lock"
        mechanism = "control policy/teardown must release or time out lock holders"
    elif sig.signature == "context-growth-snapshot-loop":
        behavior = "agent repeatedly requested snapshots or carried stale page dumps"
        mechanism = "context management must prune page dumps and nudge away from repeated snapshots"
    elif sig.signature == "empty-or-truncated-model-turn":
        behavior = "model turn returned no actionable content or tool call"
        mechanism = "completion budget/retry policy must handle truncated empty turns"
    elif sig.signature == "login-account-state":
        behavior = "agent encountered missing or rejected account credentials"
        mechanism = "memory/user notification should handle account state instead of retrying forms"
    elif sig.signature == "eligibility-gate":
        behavior = "agent encountered a hard listing eligibility constraint"
        mechanism = "prompt/filter context should decide eligibility before filling forms"
    elif sig.signature == "source-url-extraction":
        behavior = "upstream extraction did not yield an actionable external URL"
        mechanism = "prompt/extractor context must preserve source URL evidence"
    elif "timeout" in low:
        terminal = "Run exceeded a time or turn budget before a terminal outcome."
    return FailureRecord(
        signature=sig.signature,
        surface=sig.surface,
        outcome=sig.outcome,
        domain=sig.domain,
        terminal_cause=terminal,
        causal_behavior=behavior,
        mechanism=mechanism,
        evidence=redact_value((text or "")[-1200:], max_string=1200),
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
    passing: list[dict[str, Any]] = []
    paths = sorted(transcript_dir.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths[:max_files]:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        outcome = _extract_outcome(text) or _outcome_from_text(text)
        if outcome in {"submitted", "already_applied", "not_eligible", "payment_required"}:
            passing.append({
                "path": str(path),
                "outcome": outcome,
                "domain": _extract_domain(text),
                "preserved_behavior": _preserved_behavior(outcome),
                "excerpt": redact_value(text[-1000:], max_string=1000),
            })
            continue
        record = build_failure_record(text[-80000:], outcome=outcome)
        records.append({
            "path": str(path),
            **asdict(record),
        })

    clusters = _cluster_records(records)
    bundle = {
        "schema": "self-improvement-evidence-v1",
        "created_at": utc_now_iso(),
        "source": str(transcript_dir),
        "record_count": len(records),
        "preserved_success_examples": passing[:8],
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


def eval_apply_harness(fixture_dir: Path | None = None) -> dict[str, Any]:
    """Offline apply-harness regression eval.

    Fixtures are declarative JSON cases. They can check the prompt generated
    for a listing, expected failure classification, and trajectory-level
    invariants such as terminal outcome, forbidden tools, required tools, and
    turn/tool budgets. This is intentionally browser-free so it can run inside
    `just check` and self-improvement verification.
    """
    fixture_dir = fixture_dir or Path(APPLY_HARNESS_EVAL_FIXTURES)
    results: list[ApplyEvalCaseResult] = []
    if not fixture_dir.exists():
        return {
            "schema": "apply-harness-eval-v1",
            "fixture_dir": str(fixture_dir),
            "passed": 0,
            "failed": 0,
            "results": [],
        }
    for path in sorted(fixture_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        failures: list[str] = []
        score = 0
        _eval_prompt_case(data, failures)
        _eval_classification_case(data, failures)
        score += _eval_trajectory_case(data, failures)
        results.append(ApplyEvalCaseResult(
            name=path.stem,
            passed=not failures,
            score=score,
            failures=failures,
        ))
    summary = {
        "schema": "apply-harness-eval-v1",
        "fixture_dir": str(fixture_dir),
        "passed": sum(1 for r in results if r.passed),
        "failed": sum(1 for r in results if not r.passed),
        "results": [asdict(r) for r in results],
    }
    _append_lineage("apply_eval", {"passed": summary["passed"], "failed": summary["failed"]})
    return summary


def record_candidate_event(event: str, payload: dict[str, Any]) -> None:
    """Append an inspectable candidate/proposal/archive event."""
    _append_lineage(f"candidate.{event}", payload)


def candidate_history(*, limit: int = 20) -> list[dict[str, Any]]:
    try:
        rows = []
        with LINEAGE_LOG.open(encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if str(rec.get("event") or "").startswith("candidate."):
                    rows.append(rec)
        return rows[-limit:]
    except OSError:
        return []


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
            examples=[_cluster_example(i) for i in items[:5]],
        ))
    clusters.sort(key=lambda c: (-c.count, c.signature))
    return clusters


def _cluster_example(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        "path": rec.get("path", ""),
        "domain": rec.get("domain", ""),
        "terminal_cause": rec.get("terminal_cause", ""),
        "causal_behavior": rec.get("causal_behavior", ""),
        "mechanism": rec.get("mechanism", ""),
        "evidence": rec.get("evidence", ""),
    }


def _preserved_behavior(outcome: str) -> str:
    return {
        "submitted": "successful apply flow reached a confirmed submission",
        "already_applied": "duplicate prevention stopped before resubmitting",
        "not_eligible": "eligibility/rent hard stop prevented unnecessary form work",
        "payment_required": "paid gate was recognized without submitting payment",
    }.get(outcome, "terminal behavior should be preserved")


def _eval_prompt_case(data: dict[str, Any], failures: list[str]) -> None:
    prompt_case = data.get("prompt")
    if not isinstance(prompt_case, dict):
        return
    try:
        from .apply import build_prompt

        prompt = build_prompt(prompt_case.get("listing") or {})
    except Exception as e:  # noqa: BLE001 - eval should report, not crash
        failures.append(f"prompt build failed: {type(e).__name__}: {e}")
        return
    for needle in prompt_case.get("must_contain") or []:
        if str(needle) not in prompt:
            failures.append(f"prompt missing {needle!r}")
    for needle in prompt_case.get("must_not_contain") or []:
        if str(needle) in prompt:
            failures.append(f"prompt unexpectedly contains {needle!r}")


def _eval_classification_case(data: dict[str, Any], failures: list[str]) -> None:
    case = data.get("classification")
    if not isinstance(case, dict):
        return
    sig = classify_failure(
        str(case.get("text") or ""),
        outcome=str(case.get("outcome") or ""),
        domain=str(case.get("domain") or ""),
    )
    if case.get("expected_signature") and sig.signature != case.get("expected_signature"):
        failures.append(f"classification signature {sig.signature!r} != {case.get('expected_signature')!r}")
    if case.get("expected_surface") and sig.surface != case.get("expected_surface"):
        failures.append(f"classification surface {sig.surface!r} != {case.get('expected_surface')!r}")


def _eval_trajectory_case(data: dict[str, Any], failures: list[str]) -> int:
    case = data.get("trajectory")
    if not isinstance(case, dict):
        return 0
    events = case.get("events") or []
    if not isinstance(events, list):
        failures.append("trajectory.events must be a list")
        return 0
    tool_names = _trajectory_tool_names(events)
    guard_names = _trajectory_guard_names(events)
    final = _trajectory_final(events)
    score = 0
    if case.get("expected_outcome") and final.get("outcome") != case.get("expected_outcome"):
        failures.append(f"final outcome {final.get('outcome')!r} != {case.get('expected_outcome')!r}")
    else:
        score += 1
    max_turns = case.get("max_turns")
    if max_turns is not None and _trajectory_turns(events) > int(max_turns):
        failures.append(f"turn count {_trajectory_turns(events)} > {max_turns}")
    else:
        score += 1
    max_tools = case.get("max_tool_calls")
    if max_tools is not None and len(tool_names) > int(max_tools):
        failures.append(f"tool call count {len(tool_names)} > {max_tools}")
    else:
        score += 1
    for name in case.get("required_tools") or []:
        if name not in tool_names:
            failures.append(f"required tool {name!r} was not called")
    for name in case.get("forbidden_tools") or []:
        if name in tool_names:
            failures.append(f"forbidden tool {name!r} was called")
    for guard in case.get("required_guards") or []:
        if guard not in guard_names:
            failures.append(f"required guard {guard!r} did not fire")
    for pattern in case.get("forbidden_action_patterns") or []:
        rx = re.compile(str(pattern), re.IGNORECASE)
        for event in events:
            text = json.dumps(event, ensure_ascii=False)
            if rx.search(text):
                failures.append(f"forbidden action pattern matched: {pattern!r}")
                break
    return score


def _trajectory_tool_names(events: list[dict[str, Any]]) -> list[str]:
    names = []
    for rec in events:
        payload = rec.get("payload") if isinstance(rec, dict) else {}
        if rec.get("event") == "tool_call" and isinstance(payload, dict):
            names.append(str(payload.get("name") or payload.get("tool") or ""))
        if rec.get("event") == "turn_usage" and isinstance(payload, dict):
            for call in payload.get("tool_calls") or []:
                if isinstance(call, dict):
                    names.append(str(call.get("name") or call.get("tool") or ""))
    return [n for n in names if n]


def _trajectory_guard_names(events: list[dict[str, Any]]) -> list[str]:
    out = []
    for rec in events:
        if rec.get("event") != "guard":
            continue
        payload = rec.get("payload") if isinstance(rec, dict) else {}
        if isinstance(payload, dict):
            out.append(str(payload.get("name") or payload.get("guard") or payload.get("type") or ""))
    return [g for g in out if g]


def _trajectory_final(events: list[dict[str, Any]]) -> dict[str, Any]:
    for rec in reversed(events):
        if rec.get("event") in {"final", "result", "outcome"}:
            payload = rec.get("payload") if isinstance(rec, dict) else {}
            return payload if isinstance(payload, dict) else {}
    return {}


def _trajectory_turns(events: list[dict[str, Any]]) -> int:
    turns = 0
    for rec in events:
        payload = rec.get("payload") if isinstance(rec, dict) else {}
        if isinstance(payload, dict) and payload.get("turn") is not None:
            try:
                turns = max(turns, int(payload.get("turn")))
            except (TypeError, ValueError):
                pass
        if rec.get("event") == "turn_usage":
            turns += 1
    return turns


def changes_touch_apply_harness(changed_paths: list[str]) -> bool:
    patterns = (
        "src/browser_agent.py",
        "src/browser_dom_tools.py",
        "src/apply.py",
        "src/message_template.py",
        "src/site_playbooks.py",
        "src/known_gates.py",
        "src/self_improvement_harness.py",
        "tests/fixtures/apply_harness_eval/*",
    )
    return any(any(fnmatch.fnmatch(path, pat) for pat in patterns)
               for path in changed_paths)


def _append_lineage(event: str, payload: dict[str, Any]) -> None:
    try:
        LINEAGE_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": utc_now_iso(),
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
        from .redaction import redact

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
    sub.add_parser("apply-eval")
    args = parser.parse_args(argv)
    if args.cmd == "mine":
        return _print_json(mine_failures(transcript_dir=args.transcripts, max_files=args.max_files))
    if args.cmd == "eval":
        summary = eval_harness()
        _print_json(summary)
        return 1 if summary.get("failed") else 0
    if args.cmd == "apply-eval":
        summary = eval_apply_harness()
        _print_json(summary)
        return 1 if summary.get("failed") else 0
    parser.error(f"unknown command {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
