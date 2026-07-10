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
from dataclasses import dataclass
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

from . import eventlog, incident_store, known_gates, self_improvement_harness
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
# read-only tools on a small budget; phase 2 (only on a "fix" verdict)
# patches with the FULL SELF_IMPROVEMENT_MAX_TURNS budget to itself.
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
    r"\bgit\s+(commit|push|reset\b|checkout\s+--|clean\s+-f)\b",
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
    """Shared engine for every self-improvement trigger (apply failures AND
    poller zero-yield). Incident memory (src/incident_store.py): fingerprint,
    skip if this incident already had a run in the dedup window, and feed a
    run that does happen its predecessors' findings. Fail-open: a broken store
    never blocks self-improvement, and self-improvement never raises into the
    pipeline that called it.
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
        # Log the FULL traceback (not just str(e)): the crash email only carried
        # "AttributeError: 'dict' ..." with no frames, which made the 08-07-2026
        # version-skew crash hard to place.
        _log("error", status=status_label, error=f"{type(e).__name__}: {e}",
             traceback=traceback.format_exc()[-3000:])
        if fp is not None:
            try:
                incident_store.record_attempt(
                    fp, action="error", summary=f"{type(e).__name__}: {e}")
            except Exception:
                pass
        try:
            send_alert("⚠️ Self-improvement agent failed",
                       f"{crash_detail}\n\n{type(e).__name__}: {e}")
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
        return {
            "kind": "apply",
            "listing": listing,
            "result": {
                "outcome": result.outcome,
                "rc": result.rc,
                "summary": result.summary,
                "transcript_path": result.transcript_path,
            },
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


def improve_poller_zero_yield(
    *,
    site_name: str,
    list_url: str = "",
    tier: int = 0,
    parser_desc: str = "",
    sample_path: str = "",
    streak: int = 0,
) -> SelfImprovementResult | None:
    """Close the loop on a silently-broken poller parser: a site that has
    yielded zero listings for many consecutive polls almost always means its
    parser stopped matching the site's markup (URL scheme changed, JSON-LD
    dropped, now behind login). Instead of emailing a human to run
    `just poll-once` and fix it, drive the same two-phase diagnose→patch→
    deploy engine against the saved sample HTML. Returns None only when
    self-improvement is disabled entirely (so the caller can fall back to the
    old alert); otherwise always returns a result (possibly a dedup skip)."""
    if not SELF_IMPROVEMENT_ENABLED:
        return None
    fp = None
    try:
        fp = incident_store.fingerprint_poller_zero_yield(site_name)
    except Exception as e:  # noqa: BLE001
        _log("incident_store_error", status="poller_zero_yield",
             error=f"{type(e).__name__}: {e}")

    summary = (f"Poller site {site_name} yielded 0 listings for {streak} "
               f"consecutive polls ({list_url}); its parser is likely broken.")

    def _ctx(incident: dict) -> dict:
        return {
            "kind": "poller_zero_yield",
            "result": {
                "outcome": "poller_zero_yield", "rc": 0,
                "summary": summary, "transcript_path": "",
            },
            "poller": {
                "site_name": site_name, "list_url": list_url, "tier": tier,
                "parser_desc": parser_desc, "sample_path": sample_path,
                "streak": streak,
            },
            "trigger": "poller_zero_yield",
            "msg_id": None,
            "extra": {},
            "incident": incident,
        }

    return _run_for_incident(
        fp=fp, ctx_builder=_ctx, status_label="poller_zero_yield",
        occurrence_summary=summary,
        crash_detail=(f"The self-improvement agent crashed while handling a "
                      f"zero-yield for poller site {site_name}."),
    )


def improve_exception(
    *,
    listing: dict,
    error: Exception,
    trigger: str,
    msg_id: str | None = None,
    extra: dict | None = None,
) -> SelfImprovementResult | None:
    result = AgentResult(
        rc=2,
        outcome="error",
        summary=f"{type(error).__name__}: {error}",
    )
    return improve_after_apply(
        listing=listing,
        result=result,
        trigger=trigger,
        msg_id=msg_id,
        extra=extra,
    )


def run_self_improvement(context: dict) -> SelfImprovementResult:
    log_path = _new_log_path()
    logger = _Logger(log_path)
    try:
        logger.line(f"[self-improvement] model={SELF_IMPROVEMENT_PROXY_MODEL} status={context['result']['outcome']}")
        rr = asyncio.run(asyncio.wait_for(
            _execute(context, logger),
            timeout=SELF_IMPROVEMENT_TIMEOUT_SECONDS,
        ))
        rr.log_path = str(log_path)
        return rr
    except TimeoutError:
        return SelfImprovementResult(action="timeout",
                                     summary="Self-improvement agent timed out.",
                                     log_path=str(log_path))
    finally:
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
    logger.line(f"[self-improvement] worktree {worktree_path} on branch {branch_name}")
    try:
        system_prompt = (
            "You are the self-improvement agent for a Dutch rental-application "
            "bot. You run after an apply attempt ends with a non-terminal "
            "outcome, working in an isolated git worktree checked out from "
            "origin/main -- a fix you commit here does not touch the live "
            "checkout directly; commit_push_deploy pushes it to main (or a "
            "review branch) for the existing CI/CD pipeline to deploy."
        )
        mcp_servers: dict[str, Any] = {
            "browser": _browser_tools(),
            "self_improve": _self_improve_tools(context, logger, worktree_path, branch_name),
        }

        def _options(allowed_tools: list[str], max_turns: int) -> ClaudeAgentOptions:
            return ClaudeAgentOptions(
                cwd=str(worktree_path),
                system_prompt=system_prompt,
                allowed_tools=allowed_tools,
                disallowed_tools=["WebSearch", "WebFetch"],
                permission_mode="bypassPermissions",
                can_use_tool=_can_use_tool,
                setting_sources=[],
                mcp_servers=mcp_servers,
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
            allowed_tools=[
                "Read", "Grep", "Glob", "Bash",
                "mcp__browser__browser_open", "mcp__browser__browser_diagnostics",
                "mcp__browser__browser_safe_click", "mcp__browser__browser_screenshot",
                "mcp__self_improve__send_user_email",
                "mcp__self_improve__record_known_gate",
            ],
            max_turns=SELF_IMPROVEMENT_DIAGNOSIS_MAX_TURNS,
        )
        result_msg = await _query_once(
            _diagnosis_prompt(context), diagnosis_options, logger, phase="diagnosis")
        if result_msg is None:
            return SelfImprovementResult(
                action="incomplete",
                summary="Diagnosis query ended without a result message.")
        diagnosis = _parse_marker(result_msg, _DIAGNOSIS_MARKER_RE)
        if not isinstance(diagnosis, dict):
            return SelfImprovementResult(
                action="incomplete",
                summary=("Diagnosis ended without a DIAGNOSIS_JSON line: "
                         + (result_msg.result or "no result text")[:1500]))

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
        )
    finally:
        _remove_worktree(worktree_path, branch_name, logger)


async def _run_patch_candidates(
    *,
    context: dict,
    diagnosis: dict,
    root_cause: str,
    logger: _Logger,
    options_factory,
    worktree_path: Path,
) -> SelfImprovementResult:
    patch_options = options_factory(
        allowed_tools=[
            "Read", "Edit", "Write", "Grep", "Glob", "Bash",
            "mcp__browser__browser_open", "mcp__browser__browser_diagnostics",
            "mcp__browser__browser_safe_click", "mcp__browser__browser_screenshot",
            "mcp__self_improve__run_verification",
            "mcp__self_improve__commit_push_deploy",
            "mcp__self_improve__send_user_email",
            "mcp__self_improve__record_known_gate",
        ],
        max_turns=SELF_IMPROVEMENT_MAX_TURNS,
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
        result_msg = await _query_once(
            _patch_prompt(context, diagnosis, candidate_strategy=strategy),
            patch_options,
            logger,
            phase=f"patch:{strategy}",
        )
        if result_msg is None:
            last = SelfImprovementResult(
                action="fix_failed", root_cause=root_cause,
                summary="Patch query ended without a result message.",
                strategy=strategy, candidate_id=candidate_id)
        else:
            last = _parse_result(result_msg)
            if not last.root_cause:
                last.root_cause = root_cause
            last.strategy = strategy
            last.candidate_id = candidate_id
        self_improvement_harness.record_candidate_event("result", {
            "candidate_id": candidate_id,
            "strategy": strategy,
            "action": last.action,
            "root_cause": last.root_cause,
            "summary": last.summary,
            "code_changed": last.code_changed,
            "deployed": last.deployed,
        })
        if last.action == "fixed_deployed" or last.deployed or last.code_changed:
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


async def _can_use_tool(
    tool_name: str,
    tool_input: dict,
    ctx: ToolPermissionContext,
) -> PermissionResultAllow | PermissionResultDeny:
    if tool_name == "Bash":
        command = str((tool_input or {}).get("command") or "")
        if _DANGEROUS_BASH_RE.search(command):
            return PermissionResultDeny(
                message=(
                    "Direct git commit/push/reset via Bash is not allowed. Use the "
                    "commit_push_deploy tool instead — it enforces verification and "
                    "the configured push-to-main-or-review-branch policy."
                ),
            )
    return PermissionResultAllow()


def _self_improve_tools(
    context: dict, logger: _Logger, worktree_path: Path, branch_name: str,
) -> McpSdkServerConfig:
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
        return {"content": [{"type": "text", "text": confirmation}]}

    return create_sdk_mcp_server(
        name="self_improve",
        tools=[run_verification, commit_push_deploy, send_user_email,
               record_known_gate],
    )


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
