"""Autonomous diagnosis and self-improvement for failed apply attempts.

The normal application agent handles the rental site. This module handles the
agent itself after an unsuccessful run: inspect redacted logs, decide whether
the cause is an external/user-action state or a code bug, and then either do
nothing, email the user, or patch + verify + commit + push + deploy.

Driven by the Claude Agent SDK (the engine behind Claude Code) so diagnosis
and patching use real Read/Edit/Write/Grep/Glob/Bash tools instead of
hand-rolled reimplementations of them.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import traceback
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    McpSdkServerConfig,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

from . import (
    eventlog,
    incident_store,
    known_gates,
    self_improvement_harness,
    self_improvement_queue,
)
from .self_improvement.browser_tools import (
    _blocked_click_label as _blocked_click_label,  # re-export: tests target this module
    _browser_tools,
    _safe_browser_url as _safe_browser_url,  # re-export: tests target this module
)
from .self_improvement.cost import _estimate_deepseek_cost_usd
from .self_improvement.prompts import _diagnosis_prompt, _patch_prompt
from .self_improvement.util import _redacted
from .self_improvement.worktree import (
    WORKTREE_BASE as WORKTREE_BASE,  # re-export: patched via this module in tests
    _create_worktree,
    _remove_worktree,
)
from . import settings as settings_module
from .settings import settings
from .browser_agent import AgentResult
from .config import LOG_DIR, PROJECT_ROOT
from .dashboard.data import redact
from .notify import send_alert


# All SELF_IMPROVEMENT_* env knobs (with their RECOVERY_* compatibility
# aliases) are declared and parsed in src/settings.py; the comments on WHY
# each default is what it is live there and in AGENTS.md.
SELF_IMPROVEMENT_ENABLED = settings().self_improvement_enabled
# Routed through a local LiteLLM proxy (deploy/litellm.config.yaml) backed by
# DeepSeek, not the real Anthropic API -- see AGENTS.md gotchas for why
# thinking/effort/output_format are not used on this path.
SELF_IMPROVEMENT_BASE_URL = settings().self_improvement_base_url
SELF_IMPROVEMENT_PROXY_MODEL = settings().self_improvement_proxy_model
SELF_IMPROVEMENT_MAX_TURNS = settings().self_improvement_max_turns
# The run is split in two phases (verified need: 3 production runs died at
# "Reached maximum number of turns (30)" because ONE budget had to cover
# read-conventions + diagnose + patch + verify). Phase 1 diagnoses with
# read-only tools on a bounded budget; phase 2 (only on a "fix" verdict)
# patches with the FULL SELF_IMPROVEMENT_MAX_TURNS budget to itself. Twenty
# turns gives diagnosis enough room to verify a direct causal error and submit
# its authoritative verdict, while the prompt explicitly stops broad research
# once that evidence exists.
SELF_IMPROVEMENT_DIAGNOSIS_MAX_TURNS = settings().self_improvement_diagnosis_max_turns
# ClaudeAgentOptions.max_budget_usd is enforced against the SDK's own
# *client-side* cost estimate, which doesn't recognize a proxied model_name
# and inflates real DeepSeek-v4-pro spend by ~19.5x (verified: a run that
# really cost $0.03 was internally accounted as $0.586). Scaled up to match,
# or a harder run would get cut short on inflated-phantom budget, not real
# spend. Real spend is what _estimate_deepseek_cost_usd logs, not this cap.
SELF_IMPROVEMENT_MAX_BUDGET_USD = settings().self_improvement_max_budget_usd
# Wall-clock cap for BOTH phases together (diagnosis + optional patch).
SELF_IMPROVEMENT_TIMEOUT_SECONDS = settings().self_improvement_timeout_seconds
SELF_IMPROVEMENT_VERIFY_CMD = settings().self_improvement_verify_cmd
SELF_IMPROVEMENT_ALLOW_CODE_CHANGES = settings().self_improvement_allow_code_changes
# Gates whether a verified fix is pushed straight to `main` (where the
# existing CI/CD pipeline -- ci.yml -> deploy.yml -- deploys it
# automatically) or to a review branch for a human to merge by hand. There is
# no separate local deploy script anymore; pushing to `main` *is* the deploy
# trigger. SELF_IMPROVEMENT_ALLOW_DIRTY_WORKTREE / _REQUIRE_MAIN /
# _DEPLOY_CMD no longer apply -- work always happens in a fresh worktree
# branched from a freshly-fetched origin/main (see _create_worktree), so
# there's no shared/dirty checkout and no other branch it could be based on.
SELF_IMPROVEMENT_ALLOW_DEPLOY = settings().self_improvement_allow_deploy
SELF_IMPROVEMENT_PROPOSAL_CANDIDATES = settings().self_improvement_proposal_candidates


DEFAULT_SELF_IMPROVEMENT_OUTCOMES = set(
    settings_module.DEFAULT_SELF_IMPROVEMENT_OUTCOMES)
SELF_IMPROVEMENT_OUTCOMES = set(settings().self_improvement_outcomes)

RUN_LOG = LOG_DIR / "self_improvement.jsonl"
_MAX_TOOL_TEXT = 30000

# `output_config.format` (structured output) is silently ignored by DeepSeek
# via the LiteLLM proxy -- no error, just a free-text reply instead of
# schema-JSON -- so the final result is a text marker parsed with this regex,
# same convention the pre-Claude-Agent-SDK engine used.
_RESULT_MARKER_RE = re.compile(r"SELF_IMPROVEMENT_JSON:\s*(\{.*\})", re.DOTALL)
_DIAGNOSIS_MARKER_RE = re.compile(r"DIAGNOSIS_JSON:\s*(\{.*\})", re.DOTALL)

# Belt-and-suspenders: even though commit_push_deploy is the only tool that can
# actually write git history, deny raw git-write/deploy attempts via Bash too
# so enforcement doesn't depend on the model choosing the right tool.
_DANGEROUS_BASH_RE = re.compile(
    r"\bgit\s+(add|branch|switch|checkout|merge|rebase|stash|commit|push|"
    r"reset|clean|tag|cherry-pick|revert)\b",
    re.IGNORECASE,
)


@dataclass
class SelfImprovementResult:
    action: str
    summary: str
    root_cause: str = ""
    email_sent: bool = False
    code_changed: bool = False
    deployed: bool = False
    log_path: str = ""  # per-run text log, so the dashboard can link to it
    strategy: str = ""
    candidate_id: str = ""


@dataclass
class _RunToolState:
    """Authoritative state written by tools before the model can time out.

    Model-authored JSON remains a compatibility fallback.  A successfully
    recorded diagnosis, gate, email, commit, or push must survive a later SDK
    max-turn/control error.
    """

    diagnosis: dict[str, Any] | None = None
    terminal: SelfImprovementResult | None = None


def should_recover(status: str | None) -> bool:
    return SELF_IMPROVEMENT_ENABLED and (status or "") in SELF_IMPROVEMENT_OUTCOMES


def should_improve(status: str | None) -> bool:
    return should_recover(status)


def _run_for_incident(
    *,
    fp,
    ctx_builder,
    status_label: str,
    crash_detail: str,
    occurrence_listing: dict | None = None,
    occurrence_summary: str = "",
) -> SelfImprovementResult:
    """Shared engine for every self-improvement trigger. Incident memory
    (src/incident_store.py): fingerprint, skip if this incident already had a
    run in the dedup window, and feed a run that does happen its
    predecessors' findings. Fail-open: a broken store never blocks
    self-improvement, and self-improvement never raises into the pipeline
    that called it.
    """
    incident: dict = {}
    try:
        if fp is not None:
            post_deploy = incident_store.post_deploy_status(fp)
            allowed, reason = incident_store.should_run(fp)
            current_is_post_deploy_recurrence = bool(
                post_deploy.get("recurred") or post_deploy.get("latest_deploy_within_window")
            )
            if not allowed and current_is_post_deploy_recurrence:
                allowed = True
                reason = (
                    f"incident {fp.key} recurred after deployed fix "
                    f"{post_deploy.get('candidate_id') or post_deploy.get('deployed_at')}; "
                    "running again with a different proposal strategy"
                )
            incident_store.record_occurrence(fp, listing=occurrence_listing,
                                             summary=occurrence_summary, ran=allowed)
            if not allowed:
                _log("skipped_duplicate_incident", status=status_label,
                     fingerprint=fp.key, reason=reason)
                return SelfImprovementResult(action="skipped_duplicate_incident",
                                             summary=reason)
            incident = {
                "fingerprint": fp.key,
                "occurrences": incident_store.occurrence_count(fp),
                "prior_attempts": incident_store.attempt_history(fp),
                "post_deploy": post_deploy,
                "scheduler_reason": reason,
                "recent_candidates": self_improvement_harness.candidate_history(limit=8),
            }
    except Exception as e:  # noqa: BLE001 - incident memory is best-effort
        _log("incident_store_error", status=status_label,
             error=f"{type(e).__name__}: {e}")

    ctx = ctx_builder(incident)
    try:
        rr = run_self_improvement(ctx)
        if not isinstance(rr, SelfImprovementResult):
            # Should be unreachable, but a deploy-time module version skew
            # (late import loading a torn mix of old/new code mid-restart) once
            # produced a bare dict here, crashing with an opaque
            # AttributeError. Coerce instead of crashing, and record it.
            _log("error", status=status_label,
                 error=f"run_self_improvement returned {type(rr).__name__}, not "
                       "SelfImprovementResult (likely deploy-time version skew)")
            return SelfImprovementResult(
                action="error",
                summary=f"internal: unexpected result type {type(rr).__name__}")
        _log("done", status=status_label, action=rr.action,
             code_changed=rr.code_changed, deployed=rr.deployed,
             email_sent=rr.email_sent, root_cause=rr.root_cause,
             summary=rr.summary, log_path=rr.log_path)
        if fp is not None:
            try:
                incident_store.record_attempt(
                    fp, action=rr.action, root_cause=rr.root_cause,
                    summary=rr.summary, code_changed=rr.code_changed,
                    deployed=rr.deployed, strategy=rr.strategy,
                    candidate_id=rr.candidate_id)
            except Exception:
                pass
        return rr
    except Exception as e:  # noqa: BLE001 - self-improvement must be best-effort
        # Log the FULL traceback (not just str(e)): a bare "AttributeError:
        # 'dict' ..." with no frames made the 08-07-2026 version-skew crash
        # hard to place. No per-crash email: the healthcheck already alerts
        # when the last SELF_IMPROVEMENT_HEALTH_WINDOW runs all failed, and
        # per-crash mails were pure noise on top of that (removed 10-07-2026).
        _log("error", status=status_label, detail=crash_detail,
             error=f"{type(e).__name__}: {e}",
             traceback=traceback.format_exc()[-3000:])
        if fp is not None:
            try:
                incident_store.record_attempt(
                    fp, action="error", summary=f"{type(e).__name__}: {e}")
            except Exception:
                pass
        return SelfImprovementResult(action="error", summary=f"{type(e).__name__}: {e}")


def improve_after_apply(
    *,
    listing: dict,
    result: AgentResult,
    trigger: str,
    msg_id: str | None = None,
    extra: dict | None = None,
) -> SelfImprovementResult | None:
    """Persist a failed apply for the dedicated serial worker."""
    if not should_improve(result.outcome):
        return None
    payload = {
        "listing": listing,
        "result": {
            "rc": result.rc,
            "outcome": result.outcome,
            "summary": result.summary,
            "transcript_path": result.transcript_path,
            "resolved_url": result.resolved_url,
        },
        "trigger": trigger,
        "msg_id": msg_id,
        "extra": extra or {},
    }
    try:
        job_id = self_improvement_queue.enqueue("apply", payload)
    except Exception as exc:  # noqa: BLE001 - queue must never break apply
        _log("queue_error", status=result.outcome, trigger=trigger,
             error=f"{type(exc).__name__}: {exc}")
        return SelfImprovementResult(
            action="error",
            summary=f"Could not persist self-improvement job: {type(exc).__name__}: {exc}",
        )
    _log("queued", status=result.outcome, job_id=job_id, trigger=trigger)
    return SelfImprovementResult(
        action="queued",
        summary=f"Queued self-improvement job {job_id} for the serial worker.",
    )


def _improve_after_apply_now(
    *,
    listing: dict,
    result: AgentResult,
    trigger: str,
    msg_id: str | None = None,
    extra: dict | None = None,
) -> SelfImprovementResult | None:
    """Run self-improvement for a failed apply result when configured to do so.

    Never raises into the caller; the application pipeline must continue even if
    self-improvement fails.
    """
    if not should_improve(result.outcome):
        return None
    fp = None
    try:
        fp = incident_store.fingerprint_failure(listing, result.outcome, result.summary)
    except Exception as e:  # noqa: BLE001
        _log("incident_store_error", status=result.outcome,
             error=f"{type(e).__name__}: {e}")

    def _ctx(incident: dict) -> dict:
        result_context = {
            "outcome": result.outcome,
            "rc": result.rc,
            "summary": result.summary,
            "transcript_path": result.transcript_path,
        }
        try:
            failure_record = asdict(self_improvement_harness.build_failure_record(
                json.dumps(
                    {"result": result_context, "extra": extra or {}},
                    ensure_ascii=False,
                    default=str,
                ),
                outcome=result.outcome,
            ))
        except Exception:  # noqa: BLE001 - evidence is fail-open
            failure_record = {}
        return {
            "kind": "apply",
            "listing": listing,
            "result": result_context,
            "failure_record": failure_record,
            "trigger": trigger,
            "msg_id": msg_id,
            "extra": extra or {},
            "incident": incident,
        }

    return _run_for_incident(
        fp=fp, ctx_builder=_ctx, status_label=result.outcome,
        occurrence_listing=listing, occurrence_summary=result.summary,
        crash_detail=(f"The self-improvement agent crashed while handling "
                      f"{result.outcome}.\nListing: {listing.get('source_url') or '-'}"),
    )


def improve_session_keeper_adapter(
    *,
    domain: str,
    detail: str,
    probe_url: str = "",
    login_url: str = "",
) -> SelfImprovementResult | None:
    """Close the loop on a session-keeper login adapter that ran to
    completion but did not restore a logged-in session -- almost always
    because the site's login page changed and the adapter's selectors/button
    text in src/session_keeper.py are stale. Only called for that ONE failure
    class (see session_keeper._feed_self_improvement): CAPTCHA, 2FA, account-
    chooser mismatches, and rejected passwords are real external gates only a
    human can clear, and never reach here. Returns None only when
    self-improvement is disabled entirely; otherwise always returns a result
    (possibly a dedup skip)."""
    if not SELF_IMPROVEMENT_ENABLED:
        return None
    payload = {
        "domain": domain, "detail": detail,
        "probe_url": probe_url, "login_url": login_url,
    }
    try:
        job_id = self_improvement_queue.enqueue("session_keeper_adapter", payload)
    except Exception as exc:  # noqa: BLE001 - caller falls back to alert
        _log("queue_error", status="session_keeper_adapter",
             trigger="session_keeper", error=f"{type(exc).__name__}: {exc}")
        return None
    _log("queued", status="session_keeper_adapter", job_id=job_id,
         trigger="session_keeper")
    return SelfImprovementResult(
        action="queued",
        summary=f"Queued self-improvement job {job_id} for the serial worker.",
    )


def _improve_session_keeper_adapter_now(
    *,
    domain: str,
    detail: str,
    probe_url: str = "",
    login_url: str = "",
) -> SelfImprovementResult | None:
    if not SELF_IMPROVEMENT_ENABLED:
        return None
    fp = None
    try:
        fp = incident_store.fingerprint_session_keeper_adapter(domain)
    except Exception as e:  # noqa: BLE001
        _log("incident_store_error", status="session_keeper_adapter",
             error=f"{type(e).__name__}: {e}")

    summary = (f"session_keeper's login adapter for {domain} completed a "
              f"repair attempt but the session was not restored: {detail}")

    def _ctx(incident: dict) -> dict:
        return {
            "kind": "session_keeper_adapter",
            "result": {
                "outcome": "session_keeper_adapter_broken", "rc": 0,
                "summary": summary, "transcript_path": "",
            },
            "session_keeper": {
                "domain": domain, "detail": detail,
                "probe_url": probe_url, "login_url": login_url,
            },
            "trigger": "session_keeper",
            "msg_id": None,
            "extra": {},
            "incident": incident,
        }

    return _run_for_incident(
        fp=fp, ctx_builder=_ctx, status_label="session_keeper_adapter_broken",
        occurrence_summary=summary,
        crash_detail=(f"The self-improvement agent crashed while handling a "
                      f"broken session-keeper adapter for {domain}."),
    )


def improve_exception(
    *,
    listing: dict,
    error: Exception,
    trigger: str,
    msg_id: str | None = None,
    extra: dict | None = None,
) -> SelfImprovementResult | None:
    evidence = _format_exception_evidence(error)
    result = AgentResult(
        rc=2,
        outcome="error",
        summary=evidence,
    )
    return improve_after_apply(
        listing=listing,
        result=result,
        trigger=trigger,
        msg_id=msg_id,
        extra={**(extra or {}), "exception_evidence": evidence},
    )


def process_queued_job(job: dict[str, Any]) -> SelfImprovementResult | None:
    """Execute one durable job. Called only by the serial worker."""
    kind = str(job.get("kind") or "")
    payload = job.get("payload") or {}
    if kind == "apply":
        raw = payload.get("result") or {}
        result = AgentResult(
            rc=int(raw.get("rc") or 2),
            outcome=str(raw.get("outcome") or "error"),
            summary=str(raw.get("summary") or ""),
            transcript_path=str(raw.get("transcript_path") or ""),
            resolved_url=str(raw.get("resolved_url") or ""),
        )
        return _improve_after_apply_now(
            listing=payload.get("listing") or {},
            result=result,
            trigger=str(payload.get("trigger") or "unknown"),
            msg_id=payload.get("msg_id"),
            extra=payload.get("extra") or {},
        )
    if kind == "session_keeper_adapter":
        return _improve_session_keeper_adapter_now(
            domain=str(payload.get("domain") or ""),
            detail=str(payload.get("detail") or ""),
            probe_url=str(payload.get("probe_url") or ""),
            login_url=str(payload.get("login_url") or ""),
        )
    raise ValueError(f"unknown self-improvement job kind: {kind!r}")


def _format_exception_evidence(error: BaseException) -> str:
    """Keep nested ExceptionGroup causes instead of only its generic title."""
    rendered = "".join(traceback.format_exception(error)).strip()
    if not rendered:
        rendered = f"{type(error).__name__}: {error}"
    return redact(rendered)[-20_000:]


def record_abandoned_runs() -> list[str]:
    """Close run starts left terminal-less by SIGTERM/deploy/host death."""
    starts: set[str] = set()
    finished: set[str] = set()
    try:
        with RUN_LOG.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                run_id = str(rec.get("run_id") or "")
                if rec.get("event") == "run_started" and run_id:
                    starts.add(run_id)
                elif rec.get("event") == "run_finished" and run_id:
                    finished.add(run_id)
    except OSError:
        return []
    abandoned = sorted(starts - finished)
    for run_id in abandoned:
        _log("run_abandoned", run_id=run_id,
             reason="worker recovered after prior process exited without terminal record")
        _log("run_finished", run_id=run_id, action="orphan_recovered", terminal=True)
    return abandoned


def run_self_improvement(context: dict) -> SelfImprovementResult:
    log_path = _new_log_path()
    run_id = log_path.stem
    context = {**context, "run_id": run_id}
    logger = _Logger(log_path)
    _log("run_started", run_id=run_id, status=context["result"]["outcome"],
         log_path=str(log_path))
    rr: SelfImprovementResult | None = None
    try:
        logger.line(f"[self-improvement] model={SELF_IMPROVEMENT_PROXY_MODEL} status={context['result']['outcome']}")
        rr = asyncio.run(asyncio.wait_for(
            _execute(context, logger),
            timeout=SELF_IMPROVEMENT_TIMEOUT_SECONDS,
        ))
        rr.log_path = str(log_path)
        return rr
    except TimeoutError:
        rr = SelfImprovementResult(action="timeout",
                                   summary="Self-improvement agent timed out.",
                                   log_path=str(log_path))
        return rr
    except Exception:
        _log("run_finished", run_id=run_id, action="error", terminal=True)
        raise
    finally:
        if rr is not None:
            _log("run_finished", run_id=run_id, action=rr.action, terminal=True,
                 code_changed=rr.code_changed, deployed=rr.deployed)
        logger.close()


async def _execute(context: dict, logger: _Logger) -> SelfImprovementResult:
    if not settings().deepseek_api_key:
        logger.line("[self-improvement] DEEPSEEK_API_KEY not set; emailing user")
        send_alert(
            "⚠️ Rental self-improvement needs configuration",
            "The self-improvement agent could not run because DEEPSEEK_API_KEY is not "
            "set (the LiteLLM proxy it talks to needs it).",
        )
        return SelfImprovementResult(
            action="emailed_user",
            summary="DEEPSEEK_API_KEY is missing.",
            root_cause="missing_api_key",
            email_sent=True,
        )

    if not await _proxy_reachable():
        logger.line(f"[self-improvement] LiteLLM proxy unreachable at {SELF_IMPROVEMENT_BASE_URL}; emailing user")
        send_alert(
            "⚠️ Rental self-improvement needs configuration",
            "The self-improvement agent could not reach the LiteLLM proxy at "
            f"{SELF_IMPROVEMENT_BASE_URL}. Is litellm-proxy.service running?",
        )
        return SelfImprovementResult(
            action="emailed_user",
            summary="LiteLLM proxy unreachable.",
            root_cause="proxy_unreachable",
            email_sent=True,
        )

    worktree_path, branch_name = _create_worktree()
    run_id = str(context.get("run_id") or "")
    _log("worktree_created", run_id=run_id, path=str(worktree_path), branch=branch_name)
    logger.line(f"[self-improvement] worktree {worktree_path} on branch {branch_name}")
    try:
        system_prompt = (
            "You are the self-improvement agent for a Dutch rental-application "
            "bot. You run after an apply attempt ends with a non-terminal "
            "outcome, working in an isolated git worktree checked out from "
            "origin/main -- a fix you commit here does not touch the live "
            "checkout directly; commit_push_deploy pushes it to main (or a "
            "review branch) for the existing CI/CD pipeline to deploy. "
            f"Your ONLY writable repository is {worktree_path}. The live "
            f"checkout {PROJECT_ROOT} is evidence-only and MUST NEVER be "
            "edited, staged, or used as a command working directory."
        )
        tool_state = _RunToolState()
        mcp_servers: dict[str, Any] = {
            "browser": _browser_tools(),
            "self_improve": _self_improve_tools(
                context, logger, worktree_path, branch_name, tool_state),
        }

        def _options(
            built_in_tools: list[str], mcp_tools: list[str], max_turns: int,
            phase: str,
        ) -> ClaudeAgentOptions:
            return ClaudeAgentOptions(
                cwd=str(worktree_path),
                system_prompt=system_prompt,
                # `allowed_tools` only auto-approves; `tools` is the actual
                # availability boundary in claude-agent-sdk 0.2.110.
                tools=built_in_tools,
                # MCP tools are fixed local operations and may auto-run.
                # Built-ins deliberately go through can_use_tool on every call
                # so Edit/Write path checks are enforcement, not prompt advice.
                allowed_tools=mcp_tools,
                disallowed_tools=[
                    "WebSearch", "WebFetch", "Task", "TaskCreate",
                    "TaskUpdate", "TaskList", "Agent", "NotebookEdit",
                ],
                permission_mode="default",
                can_use_tool=_make_can_use_tool(worktree_path, phase),
                setting_sources=[],
                mcp_servers=mcp_servers,
                strict_mcp_config=True,
                model=SELF_IMPROVEMENT_PROXY_MODEL,
                env={
                    # Redirect Claude Code at the local LiteLLM/DeepSeek proxy
                    # instead of api.anthropic.com. ANTHROPIC_AUTH_TOKEN only
                    # needs to be non-empty to satisfy the CLI's own "am I
                    # configured" check -- the proxy doesn't validate it
                    # (confirmed with curl).
                    "ANTHROPIC_BASE_URL": SELF_IMPROVEMENT_BASE_URL,
                    "ANTHROPIC_AUTH_TOKEN": "not-required",
                },
                max_turns=max_turns,
                max_budget_usd=SELF_IMPROVEMENT_MAX_BUDGET_USD,
                # No thinking/effort/output_format here -- see AGENTS.md gotchas.
            )

        # ---- Phase 1: diagnosis (read-only; no Edit/Write, no commit tool).
        diagnosis_options = _options(
            built_in_tools=["Read", "Grep", "Glob", "Bash"],
            mcp_tools=[
                "mcp__browser__browser_open", "mcp__browser__browser_diagnostics",
                "mcp__browser__browser_safe_click", "mcp__browser__browser_screenshot",
                "mcp__self_improve__submit_diagnosis",
                "mcp__self_improve__send_user_email",
                "mcp__self_improve__record_known_gate",
            ],
            max_turns=SELF_IMPROVEMENT_DIAGNOSIS_MAX_TURNS,
            phase="diagnosis",
        )
        _log("phase_started", run_id=run_id, phase="diagnosis")
        result_msg: ResultMessage | None = None
        query_error: Exception | None = None
        try:
            result_msg = await _query_once(
                _diagnosis_prompt(context), diagnosis_options, logger, phase="diagnosis")
        except Exception as exc:  # salvage submit_diagnosis before max-turn errors
            query_error = exc
            logger.line(f"[self-improvement:diagnosis] query error: {type(exc).__name__}: {exc}")
        diagnosis = tool_state.diagnosis
        if diagnosis is None and result_msg is not None:
            diagnosis = _parse_marker(result_msg, _DIAGNOSIS_MARKER_RE)
        if query_error is not None and diagnosis is None:
            raise query_error
        if result_msg is None and diagnosis is None:
            return SelfImprovementResult(
                action="incomplete",
                summary="Diagnosis query ended without a result message.")
        if not isinstance(diagnosis, dict):
            return SelfImprovementResult(
                action="incomplete",
                summary=("Diagnosis ended without a DIAGNOSIS_JSON line: "
                         + ((result_msg.result if result_msg else "") or
                            "no result text")[:1500]))
        _log("phase_finished", run_id=run_id, phase="diagnosis",
             verdict=str(diagnosis.get("verdict") or ""), authoritative=bool(tool_state.diagnosis))

        verdict = str(diagnosis.get("verdict") or "").strip().lower()
        root_cause = str(diagnosis.get("root_cause") or "")
        summary = str(diagnosis.get("summary") or "")
        email_sent = bool(diagnosis.get("email_sent"))
        logger.line(f"[self-improvement] diagnosis verdict={verdict!r} root_cause={redact(root_cause)[:300]}")

        if verdict == "noop":
            return SelfImprovementResult(
                action="noop", root_cause=root_cause, summary=summary,
                email_sent=email_sent)
        if verdict == "email_user":
            if not email_sent:
                send_alert("⚠️ Rental agent needs attention",
                           redact(summary or root_cause or "User action required."))
                email_sent = True
            return SelfImprovementResult(
                action="emailed_user", root_cause=root_cause, summary=summary,
                email_sent=email_sent)
        if verdict != "fix":
            return SelfImprovementResult(
                action="incomplete", root_cause=root_cause,
                summary=f"Diagnosis returned unknown verdict {verdict!r}: {summary}"[:1500],
                email_sent=email_sent)

        if not SELF_IMPROVEMENT_ALLOW_CODE_CHANGES:
            send_alert(
                "🛠️ Rental bot diagnosed a fixable bug (code changes disabled)",
                redact(f"Root cause: {root_cause}\n\nPlan: "
                       f"{diagnosis.get('fix_plan') or '-'}\n\n{summary}"),
            )
            return SelfImprovementResult(
                action="fix_failed", root_cause=root_cause,
                summary="Fix verdict, but SELF_IMPROVEMENT_ALLOW_CODE_CHANGES=0; "
                        "diagnosis emailed instead.",
                email_sent=True)

        return await _run_patch_candidates(
            context=context,
            diagnosis=diagnosis,
            root_cause=root_cause,
            logger=logger,
            options_factory=_options,
            worktree_path=worktree_path,
            tool_state=tool_state,
        )
    finally:
        _remove_worktree(worktree_path, branch_name, logger)
        _log("worktree_removed", run_id=run_id, path=str(worktree_path), branch=branch_name)


async def _run_patch_candidates(
    *,
    context: dict,
    diagnosis: dict,
    root_cause: str,
    logger: _Logger,
    options_factory,
    worktree_path: Path,
    tool_state: _RunToolState,
) -> SelfImprovementResult:
    patch_options = options_factory(
        built_in_tools=["Read", "Edit", "Write", "Grep", "Glob"],
        mcp_tools=[
            "mcp__browser__browser_open", "mcp__browser__browser_diagnostics",
            "mcp__browser__browser_safe_click", "mcp__browser__browser_screenshot",
            "mcp__self_improve__run_verification",
            "mcp__self_improve__commit_push_deploy",
            "mcp__self_improve__send_user_email",
            "mcp__self_improve__record_known_gate",
        ],
        max_turns=SELF_IMPROVEMENT_MAX_TURNS,
        phase="patch",
    )
    strategies = _candidate_strategies(context, diagnosis)
    last = SelfImprovementResult(
        action="fix_failed", root_cause=root_cause,
        summary="No patch candidate ran.")
    for idx, strategy in enumerate(strategies, start=1):
        candidate_id = _candidate_id(context, idx, strategy)
        logger.line(f"[self-improvement] patch candidate {idx}/{len(strategies)} strategy={strategy} id={candidate_id}")
        self_improvement_harness.record_candidate_event("start", {
            "candidate_id": candidate_id,
            "strategy": strategy,
            "incident": (context.get("incident") or {}).get("fingerprint", ""),
            "diagnosis": diagnosis,
        })
        tool_state.terminal = None
        run_id = str(context.get("run_id") or "")
        phase = f"patch:{strategy}"
        _log("phase_started", run_id=run_id, phase=phase, candidate_id=candidate_id)
        result_msg: ResultMessage | None = None
        query_error: Exception | None = None
        try:
            result_msg = await _query_once(
                _patch_prompt(context, diagnosis, candidate_strategy=strategy),
                patch_options,
                logger,
                phase=phase,
            )
        except Exception as exc:
            query_error = exc
            logger.line(f"[self-improvement:{phase}] query error: {type(exc).__name__}: {exc}")
        if tool_state.terminal is not None:
            last = tool_state.terminal
        elif query_error is not None:
            last = SelfImprovementResult(
                action="error", root_cause=root_cause,
                summary=f"{type(query_error).__name__}: {query_error}")
        elif result_msg is None:
            last = SelfImprovementResult(
                action="fix_failed", root_cause=root_cause,
                summary="Patch query ended without a result message.",
                strategy=strategy, candidate_id=candidate_id)
        else:
            last = _parse_result(result_msg)
            last.strategy = strategy
            last.candidate_id = candidate_id
        if not last.root_cause:
            last.root_cause = root_cause
        last.strategy = strategy
        last.candidate_id = candidate_id
        _log("phase_finished", run_id=run_id, phase=phase, action=last.action,
             authoritative=bool(tool_state.terminal), candidate_id=candidate_id)
        self_improvement_harness.record_candidate_event("result", {
            "candidate_id": candidate_id,
            "strategy": strategy,
            "action": last.action,
            "root_cause": last.root_cause,
            "summary": last.summary,
            "code_changed": last.code_changed,
            "deployed": last.deployed,
        })
        if last.action in {"fixed_deployed", "fixed_review"} or last.deployed or last.code_changed:
            return last
        if idx < len(strategies):
            reset = _reset_worktree_to_head(worktree_path)
            logger.line(f"[self-improvement] reset after failed candidate: {reset[:500]}")
    return last


def _candidate_strategies(context: dict, diagnosis: dict) -> list[str]:
    incident = context.get("incident") or {}
    post_deploy = incident.get("post_deploy") or {}
    recurrent = int(incident.get("occurrences") or 0) > 1 or bool(post_deploy.get("latest_deploy_within_window"))
    surface = str(diagnosis.get("surface") or "")
    if not surface:
        try:
            record = self_improvement_harness.build_failure_record(
                json.dumps(context.get("result") or {}, ensure_ascii=False),
                outcome=str((context.get("result") or {}).get("outcome") or ""),
            )
            surface = record.surface
        except Exception:
            surface = "control_policy"
    base = {
        "prompt_context": ["prompt_context", "evaluator", "control_policy"],
        "tool_registry": ["tool_registry", "prompt_context", "evaluator"],
        "memory": ["memory", "prompt_context", "evaluator"],
        "control_policy": ["control_policy", "evaluator", "prompt_context"],
        "observability": ["observability", "evaluator", "control_policy"],
        "evaluator": ["evaluator", "control_policy", "prompt_context"],
    }.get(surface, ["control_policy", "prompt_context", "evaluator"])
    prior = {
        str(a.get("strategy") or "")
        for a in incident.get("prior_attempts") or []
        if isinstance(a, dict)
    }
    deployed_strategy = str(post_deploy.get("deployed_strategy") or "")
    avoid = prior | ({deployed_strategy} if deployed_strategy else set())
    ordered = [s for s in base if s not in avoid] + [s for s in base if s in avoid]
    count = max(1, min(SELF_IMPROVEMENT_PROPOSAL_CANDIDATES if recurrent else 1, len(ordered)))
    return ordered[:count]


def _candidate_id(context: dict, idx: int, strategy: str) -> str:
    fp = str((context.get("incident") or {}).get("fingerprint") or "incident")
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{fp}-{idx}-{strategy}")[:100]
    return f"{datetime.now().strftime('%Y%m%d_%H%M%S')}-{slug}"


def _reset_worktree_to_head(worktree_path: Path) -> str:
    # This is a disposable self-improvement worktree. Resetting it between
    # failed candidates prevents one proposal's partial edits from contaminating
    # the next proposal.
    return (
        _run_cmd(["git", "reset", "--hard", "HEAD"], timeout=30, cwd=worktree_path)
        + "\n"
        + _run_cmd(["git", "clean", "-fd"], timeout=30, cwd=worktree_path)
    )


async def _query_once(prompt: str, options: ClaudeAgentOptions,
                      logger: _Logger, *, phase: str) -> ResultMessage | None:
    result_msg: ResultMessage | None = None
    async for message in query(prompt=_one_shot_prompt(prompt), options=options):
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    logger.line(f"[self-improvement:{phase}] say: {block.text.strip()[:500]}")
                elif isinstance(block, ToolUseBlock):
                    logger.line(f"[self-improvement:{phase}] call {block.name} {_safe_args(block.input)}")
            if message.usage:
                logger.line(f"[self-improvement:{phase}] turn usage {json.dumps(message.usage, ensure_ascii=False)}")
        elif isinstance(message, ResultMessage):
            result_msg = message
    if result_msg is not None:
        # Log raw usage + both the SDK's own cost figure and an independent
        # estimate from known deepseek-v4-pro per-token rates, so cost is
        # cross-checked rather than trusted blindly (litellm's cost tracking can
        # silently default to 0/wrong rates for a custom model_name alias).
        est_cost = _estimate_deepseek_cost_usd(result_msg.usage or result_msg.model_usage or {})
        logger.line(
            f"[self-improvement:{phase}] done subtype={result_msg.subtype} is_error={result_msg.is_error} "
            f"turns={result_msg.num_turns} sdk_cost_usd={result_msg.total_cost_usd} "
            f"estimated_cost_usd={est_cost}"
        )
        logger.line(f"[self-improvement:{phase}] usage={json.dumps(result_msg.usage, ensure_ascii=False)}")
        logger.line(f"[self-improvement:{phase}] model_usage={json.dumps(result_msg.model_usage, ensure_ascii=False)}")
    return result_msg


async def _proxy_reachable() -> bool:
    import httpx

    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            r = await client.get(f"{SELF_IMPROVEMENT_BASE_URL}/health/liveliness")
            return r.status_code == 200
    except Exception:
        return False


async def _one_shot_prompt(text: str):
    """Wrap a plain prompt string as the streaming-mode input `query()` needs.

    A string prompt takes a stdin-then-close path that never opens the
    bidirectional control channel `can_use_tool` requires -- the SDK raises
    "can_use_tool callback requires streaming mode" otherwise. One yielded
    message reproduces a one-shot string query.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": text},
        "parent_tool_use_id": None,
    }


def _make_can_use_tool(worktree_path: Path, phase: str):
    root = worktree_path.resolve()

    async def guard(
        tool_name: str,
        tool_input: dict,
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        data = tool_input or {}
        if tool_name in {"Edit", "Write", "NotebookEdit"}:
            raw_path = str(data.get("file_path") or data.get("path") or "")
            candidate = Path(raw_path)
            if not candidate.is_absolute():
                candidate = root / candidate
            try:
                inside = candidate.resolve().is_relative_to(root)
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                return PermissionResultDeny(
                    message=f"Writes are restricted to isolated worktree {root}.")
        if tool_name == "Bash":
            command = str(data.get("command") or "")
            if _DANGEROUS_BASH_RE.search(command):
                return PermissionResultDeny(
                    message=(
                        "Direct git add/commit/push/reset via Bash is not allowed. "
                        "Use commit_push_deploy; it verifies and records the result."
                    ),
                )
            if phase == "patch" and str(PROJECT_ROOT) in command:
                return PermissionResultDeny(
                    message=(
                        f"Patch commands may not target live checkout {PROJECT_ROOT}; "
                        f"the command already runs in {root}."
                    ),
                )
            if phase == "patch" and re.search(r"(^|[\s/])\.\.(/|\s|$)", command):
                return PermissionResultDeny(
                    message="Patch commands may not escape the isolated worktree with '..'.")
            if phase == "diagnosis":
                check = command.replace("2>/dev/null", "").replace("2>&1", "")
                if re.search(
                    r"(?:^|[;&|]\s*|\s)(?:rm|mv|cp|install|tee|touch|mkdir|chmod|chown|truncate)\s|"
                    r"\bsed\s+-i\b|(?:^|[^>])>(?:>|[^&])",
                    check,
                ):
                    return PermissionResultDeny(
                        message="Diagnosis Bash is read-only; mutating shell commands are denied.")
        return PermissionResultAllow()

    return guard


async def _can_use_tool(
    tool_name: str,
    tool_input: dict,
    ctx: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    """Compatibility wrapper retained for tests and external imports."""
    return await _make_can_use_tool(PROJECT_ROOT, "patch")(tool_name, tool_input, ctx)


def _self_improve_tools(
    context: dict, logger: _Logger, worktree_path: Path, branch_name: str,
    state: _RunToolState,
) -> McpSdkServerConfig:
    @tool("submit_diagnosis", (
        "Submit the final diagnosis and STOP investigating. Call as soon as a "
        "root cause has direct evidence and a bounded verdict/fix plan."
    ), {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["noop", "email_user", "fix"]},
            "root_cause": {"type": "string"},
            "fix_plan": {"type": "string"},
            "summary": {"type": "string"},
            "email_sent": {"type": "boolean"},
            "surface": {"type": "string"},
        },
        "required": ["verdict", "root_cause", "fix_plan", "summary", "email_sent"],
    })
    async def submit_diagnosis(args: dict) -> dict:
        state.diagnosis = _redacted(dict(args))
        _log("tool_result", run_id=context.get("run_id"), tool="submit_diagnosis",
             verdict=state.diagnosis.get("verdict"), authoritative=True)
        return {"content": [{"type": "text", "text": (
            "Diagnosis recorded authoritatively. Stop now and return; no more "
            "source, browser, package, or deployment research is needed."
        )}]}

    @tool("run_verification", "Run the configured verification command and return its output.", {
        "type": "object",
        "properties": {},
        "required": [],
    })
    async def run_verification(args: dict) -> dict:
        text = await asyncio.to_thread(_run_shell, SELF_IMPROVEMENT_VERIFY_CMD, 300, worktree_path)
        return {"content": [{"type": "text", "text": text}]}

    @tool("commit_push_deploy", (
        "Commit all current changes in this worktree and push. If deploy is "
        "allowed and main hasn't moved, pushes straight to main (the existing "
        "CI/CD pipeline deploys it automatically); otherwise pushes a review "
        "branch instead. Refuses on policy/verification failure."
    ), {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    })
    async def commit_push_deploy(args: dict) -> dict:
        message = str(args.get("message") or "fix(self-improvement): repair apply failure")
        text = await asyncio.to_thread(
            _commit_push_deploy, message, context, worktree_path, branch_name,
        )
        state.terminal = _result_from_commit_output(text)
        _log("tool_result", run_id=context.get("run_id"),
             tool="commit_push_deploy", action=state.terminal.action,
             code_changed=state.terminal.code_changed,
             deployed=state.terminal.deployed, authoritative=True)
        return {"content": [{"type": "text", "text": text}]}

    @tool("send_user_email", "Email the configured recipient about required user action.", {
        "type": "object",
        "properties": {
            "subject": {"type": "string"},
            "body": {"type": "string"},
        },
        "required": ["subject", "body"],
    })
    async def send_user_email(args: dict) -> dict:
        subject = str(args.get("subject") or "Rental agent needs attention")
        send_alert(subject, redact(str(args.get("body") or "")))
        _log("tool_result", run_id=context.get("run_id"), tool="send_user_email",
             authoritative=True)
        return {"content": [{"type": "text", "text": "email sent"}]}

    @tool("record_known_gate", (
        "Record a durable per-site gate in state/known_gates.json so the "
        "pipeline skips (paid_registration) or warns (other kinds) "
        "deterministically on this domain from the next listing onward — a "
        "data fix that needs no code change or deploy. Kinds: "
        "paid_registration (applying costs money — pre-flight skip), "
        "account_cap (temporary account limit; set expires_ts), "
        "region_registration, delayed_access, eligibility. Use ONLY for "
        "verified external site/account gates, never for code bugs."
    ), {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "site domain, e.g. your-house.nl"},
            "kind": {"type": "string", "enum": sorted(known_gates.GATE_KINDS)},
            "note": {"type": "string", "description": "one-line evidence, e.g. '€25 membership via Mollie before applying'"},
            "expires_ts": {"type": "string", "description": "optional ISO-8601 expiry for temporary caps"},
        },
        "required": ["domain", "kind", "note"],
    })
    async def record_known_gate(args: dict) -> dict:
        try:
            confirmation = known_gates.record_gate(
                domain=str(args.get("domain") or ""),
                kind=str(args.get("kind") or ""),
                note=redact(str(args.get("note") or "")),
                source=f"self-improvement:{context.get('trigger') or '-'}",
                expires_ts=str(args.get("expires_ts") or ""),
            )
        except ValueError as e:
            return {"content": [{"type": "text", "text": f"REFUSED: {e}"}]}
        logger.line(f"[self-improvement] known gate: {confirmation}")
        _log("tool_result", run_id=context.get("run_id"), tool="record_known_gate",
             authoritative=True)
        return {"content": [{"type": "text", "text": confirmation}]}

    return create_sdk_mcp_server(
        name="self_improve",
        tools=[submit_diagnosis, run_verification, commit_push_deploy,
               send_user_email, record_known_gate],
    )


def _result_from_commit_output(text: str) -> SelfImprovementResult:
    low = (text or "").lower()
    if "pushed to main" in low:
        return SelfImprovementResult(
            action="fixed_deployed", summary=text[:2000], code_changed=True,
            deployed=True)
    if any(marker in low for marker in (
        "pushed for review", "pushed to self-improvement/", "pushed to branch",
        "instead; user email sent",
    )):
        return SelfImprovementResult(
            action="fixed_review", summary=text[:2000], code_changed=True,
            deployed=False, email_sent="email sent" in low)
    if "patch saved locally" in low:
        return SelfImprovementResult(
            action="fix_failed", summary=text[:2000], code_changed=True,
            deployed=False, email_sent="email sent" in low)
    return SelfImprovementResult(
        action="fix_failed", summary=(text or "commit/deploy tool failed")[:2000],
        code_changed=False, deployed=False)


def _commit_push_deploy(
    message: str, context: dict | None, worktree_path: Path, branch_name: str,
) -> str:
    if not SELF_IMPROVEMENT_ALLOW_CODE_CHANGES:
        return "REFUSED: SELF_IMPROVEMENT_ALLOW_CODE_CHANGES=0"
    porcelain = _porcelain(worktree_path)
    if not porcelain:
        return "nothing to commit"
    changed = _changed_paths_from_porcelain(porcelain)
    verify = _run_shell(SELF_IMPROVEMENT_VERIFY_CMD, timeout=300, cwd=worktree_path)
    if not verify.startswith("rc=0\n"):
        return "verification failed, not committing\n" + verify
    apply_eval = ""
    if self_improvement_harness.changes_touch_apply_harness(changed):
        apply_eval = _run_shell(
            "uv run python -m src.self_improvement_harness apply-eval",
            timeout=300,
            cwd=worktree_path,
        )
        if not apply_eval.startswith("rc=0\n") or '"failed": 0' not in apply_eval:
            return "apply-harness eval failed, not committing\n" + apply_eval
    add = _run_cmd(["git", "add", "-A"], timeout=30, cwd=worktree_path)
    commit = _run_cmd(["git", "commit", "-m", _commit_message(message)], timeout=60, cwd=worktree_path)
    if not commit.startswith("rc=0\n"):
        return "commit failed\n" + add + "\n" + commit

    if not SELF_IMPROVEMENT_ALLOW_DEPLOY:
        push = _run_cmd(["git", "push", "origin", f"HEAD:refs/heads/{branch_name}"],
                        timeout=120, cwd=worktree_path)
        if not push.startswith("rc=0\n"):
            return _handle_push_failure(context, worktree_path, branch_name,
                                        commit, push, target=branch_name)
        summary = (
            f"Self-improvement committed a fix on branch {branch_name} for manual "
            "review (deploy disabled -- SELF_IMPROVEMENT_ALLOW_DEPLOY=0)."
        )
        _send_fix_email(context, summary, commit + "\n" + apply_eval + "\n" + push)
        return f"committed to {branch_name}; deploy disabled; pushed for review; user email sent\n" + push

    # Re-fetch in case main moved during this run; only fast-forward, never
    # attempt automatic conflict resolution.
    _run_cmd(["git", "fetch", "origin", "main"], timeout=60, cwd=worktree_path)
    ff_check = _run_cmd(["git", "merge-base", "--is-ancestor", "origin/main", "HEAD"],
                        timeout=10, cwd=worktree_path)
    if not ff_check.startswith("rc=0"):
        push = _run_cmd(["git", "push", "origin", f"HEAD:refs/heads/{branch_name}"],
                        timeout=120, cwd=worktree_path)
        if not push.startswith("rc=0\n"):
            return _handle_push_failure(context, worktree_path, branch_name,
                                        commit, push, target=branch_name)
        summary = (
            f"Self-improvement committed a fix, but origin/main moved since this "
            f"run branched off it -- pushed to {branch_name} instead of merging "
            "automatically. Please review and merge by hand."
        )
        _send_fix_email(context, summary, commit + "\n" + apply_eval + "\n" + push)
        return f"main moved ahead; pushed to {branch_name} instead; user email sent\n" + push

    push = _run_cmd(["git", "push", "origin", "HEAD:refs/heads/main"], timeout=120, cwd=worktree_path)
    if not push.startswith("rc=0\n"):
        # Try the review branch before falling back to a local patch: a
        # branch-protection rule can reject main while branches still work.
        branch_push = _run_cmd(["git", "push", "origin", f"HEAD:refs/heads/{branch_name}"],
                               timeout=120, cwd=worktree_path)
        if branch_push.startswith("rc=0\n"):
            summary = (
                f"Self-improvement committed a fix; push to main failed but the "
                f"review branch {branch_name} was pushed. Please review and merge."
            )
            _send_fix_email(context, summary, commit + "\n" + apply_eval + "\n" + push + "\n" + branch_push)
            return (f"push to main failed; pushed to {branch_name} instead; "
                    "user email sent\n" + push)
        return _handle_push_failure(context, worktree_path, branch_name,
                                    commit, push + "\n" + branch_push, target="main")
    summary = "Self-improvement committed and pushed a fix directly to main -- CI/CD will deploy it automatically."
    _send_fix_email(context, summary, commit + "\n" + apply_eval + "\n" + push)
    return "pushed to main; CI/CD deploy triggered; user email sent\n" + push


def _changed_paths_from_porcelain(porcelain: str) -> list[str]:
    paths = []
    for line in (porcelain or "").splitlines():
        # `git status --porcelain` uses two status chars, a space, then path;
        # rename lines contain "old -> new", where the new path is what matters.
        path = line[3:].strip() if len(line) > 3 else ""
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[-1].strip()
        if path:
            paths.append(path)
    return paths


# Every verified fix used to die silently at a failed `git push` (verified in
# production 03-07-2026: the VPS deploy key was read-only, so FIVE runs each
# wrote a correct browser_lock fix and lost it; one had to be rescued from
# the worktree by hand before cleanup deleted it). The worktree is removed in
# a `finally`, so a fix that isn't pushed AND isn't saved here is gone.
PENDING_PATCH_DIR = PROJECT_ROOT / "state" / "pending_patches"


def _handle_push_failure(context: dict | None, worktree_path: Path,
                         branch_name: str, commit: str, push_output: str,
                         *, target: str) -> str:
    patch_path = _save_pending_patch(worktree_path, branch_name)
    saved = (f"Patch saved locally: {patch_path}\n"
             f"Apply with: git am {patch_path}" if patch_path
             else "Saving the patch locally ALSO failed -- the fix is lost "
                  "with the worktree; see the log for the diff.")
    summary = (
        f"Self-improvement committed a verified fix but could not push to "
        f"{target}. {saved}"
    )
    body = commit + "\n" + push_output
    if patch_path:
        try:
            body += "\n\n--- patch ---\n" + Path(patch_path).read_text(encoding="utf-8")
        except OSError:
            pass
    _send_fix_email(context, summary, body)
    return f"push to {target} failed; {saved}; user email sent\n" + push_output


def _save_pending_patch(worktree_path: Path, branch_name: str) -> str:
    try:
        r = subprocess.run(
            ["git", "format-patch", "-1", "HEAD", "--stdout"],
            cwd=worktree_path, capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return ""
        PENDING_PATCH_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", branch_name)[:80]
        path = PENDING_PATCH_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{slug}.patch"
        # The patch is the raw commit (code + message), NOT passed through
        # redact(): it must stay `git am`-able. Fix commits contain repo code,
        # never credentials; state/ is gitignored and local-only anyway.
        path.write_text(r.stdout, encoding="utf-8")
        return str(path)
    except Exception:  # noqa: BLE001 - last-resort persistence must not raise
        return ""


def _send_fix_email(context: dict | None, summary: str, details: str) -> None:
    ctx = context or {}
    listing = ctx.get("listing") or {}
    result = ctx.get("result") or {}
    subject = "🛠️ Rental bot self-improvement changed the repo"
    body = "\n".join([
        summary,
        "",
        f"Original outcome: {result.get('outcome') or '-'}",
        f"Listing: {listing.get('source_url') or listing.get('stekkies_url') or '-'}",
        f"Address: {listing.get('address') or '-'}",
        f"Source: {listing.get('source_name') or listing.get('source') or '-'}",
        "",
        "Command summary:",
        redact(details)[-_MAX_TOOL_TEXT:],
    ])
    send_alert(subject, body)


def _porcelain(cwd: Path = PROJECT_ROOT) -> str:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=cwd,
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def _run_cmd(args: list[str], timeout: int, cwd: Path = PROJECT_ROOT) -> str:
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return redact(f"rc={r.returncode}\n{r.stdout}{r.stderr}")[:_MAX_TOOL_TEXT]


def _run_shell(command: str, timeout: int, cwd: Path = PROJECT_ROOT) -> str:
    # UV_NO_SYNC guards against uv trying to reconcile the (symlinked, see
    # _create_worktree) .venv against the worktree's own lock state. Setting
    # VIRTUAL_ENV alone does NOT make uv reuse another venv -- that only
    # happens via the `--active` CLI flag (no env-var equivalent), which the
    # justfile's hardcoded `uv run` calls don't pass. Verified empirically:
    # a real self-improvement run needed a full `uv sync` before this fix.
    env = {**os.environ, "UV_NO_SYNC": "1"}
    r = subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                       shell=True, timeout=timeout, env=env)
    return redact(f"rc={r.returncode}\n{r.stdout}{r.stderr}")[:_MAX_TOOL_TEXT]


def _parse_marker(msg: ResultMessage, marker_re: re.Pattern[str]) -> dict | None:
    # DeepSeek via the LiteLLM proxy doesn't honor structured output, so the
    # final result comes from a text marker (see the phase prompts) instead
    # of msg.structured_output.
    m = marker_re.search(msg.result or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_result(msg: ResultMessage) -> SelfImprovementResult:
    data = _parse_marker(msg, _RESULT_MARKER_RE)
    if not isinstance(data, dict):
        return SelfImprovementResult(
            action="error" if msg.is_error else "unknown",
            summary=(msg.result or "no result text")[:2000],
        )
    return SelfImprovementResult(
        action=str(data.get("action") or "unknown"),
        root_cause=str(data.get("root_cause") or ""),
        summary=str(data.get("summary") or ""),
        email_sent=bool(data.get("email_sent")),
        code_changed=bool(data.get("code_changed")),
        deployed=bool(data.get("deployed")),
    )


def _safe_args(args: dict) -> str:
    try:
        rendered = json.dumps(args, ensure_ascii=False)
    except TypeError:
        rendered = str(args)
    return redact(rendered)[:300]


def _commit_message(message: str) -> str:
    first = (message or "").strip().splitlines()[0][:120]
    if not re.match(r"^(fix|chore|test|docs|refactor)(\([^)]+\))?: ", first):
        return "fix(self-improvement): repair failed application flow"
    return first


def _new_log_path() -> Path:
    path = LOG_DIR / "self_improvement" / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _log(event: str, **kw) -> None:
    rec = {"ts": eventlog.utc_now_iso(), "event": event, **_redacted(kw)}
    eventlog.append_jsonl(RUN_LOG, rec)
    print(f"[self-improvement] {event}: " + " ".join(f"{k}={v}" for k, v in rec.items() if k != "event"))


class _Logger:
    def __init__(self, path: Path):
        self.path = path
        self.fh = path.open("w", encoding="utf-8")

    def line(self, s: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        out = f"{stamp} {redact(s)}"
        print(out, flush=True)
        self.fh.write(out + "\n")
        self.fh.flush()

    def close(self) -> None:
        try:
            self.fh.close()
        except Exception:
            pass
