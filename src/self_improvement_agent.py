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

from . import incident_store, known_gates
from . import llm_pricing as _pricing
from .browser_agent import AgentResult
from .browser_dom_tools import compact, current_page, evaluate_controls, evaluate_fields
from .config import CDP_URL, LOG_DIR, PROJECT_ROOT, SCREENSHOT_DIR
from .dashboard.data import redact
from .notify import send_alert
from .poller.browser_lock import browser_lock


def _env(name: str, default: str) -> str:
    """Read SELF_IMPROVEMENT_* env vars, with RECOVERY_* as compatibility aliases."""
    legacy = "RECOVERY_" + name.removeprefix("SELF_IMPROVEMENT_")
    return os.environ.get(name, os.environ.get(legacy, default))


SELF_IMPROVEMENT_ENABLED = _env("SELF_IMPROVEMENT_ENABLED", "1") != "0"
# Routed through a local LiteLLM proxy (deploy/litellm.config.yaml) backed by
# DeepSeek, not the real Anthropic API -- see AGENTS.md gotchas for why
# thinking/effort/output_format are not used on this path.
SELF_IMPROVEMENT_BASE_URL = _env("SELF_IMPROVEMENT_BASE_URL", "http://127.0.0.1:4000")
SELF_IMPROVEMENT_PROXY_MODEL = _env("SELF_IMPROVEMENT_PROXY_MODEL", "self-improvement-deepseek")
SELF_IMPROVEMENT_MAX_TURNS = int(_env("SELF_IMPROVEMENT_MAX_TURNS", "30"))
# The run is split in two phases (verified need: 3 production runs died at
# "Reached maximum number of turns (30)" because ONE budget had to cover
# read-conventions + diagnose + patch + verify). Phase 1 diagnoses with
# read-only tools on a small budget; phase 2 (only on a "fix" verdict)
# patches with the FULL SELF_IMPROVEMENT_MAX_TURNS budget to itself.
SELF_IMPROVEMENT_DIAGNOSIS_MAX_TURNS = int(_env("SELF_IMPROVEMENT_DIAGNOSIS_MAX_TURNS", "15"))
# ClaudeAgentOptions.max_budget_usd is enforced against the SDK's own
# *client-side* cost estimate, which doesn't recognize a proxied model_name
# and inflates real DeepSeek-v4-pro spend by ~19.5x (verified: a run that
# really cost $0.03 was internally accounted as $0.586). Scaled up to match,
# or a harder run would get cut short on inflated-phantom budget, not real
# spend. Real spend is what _estimate_deepseek_cost_usd logs, not this cap.
SELF_IMPROVEMENT_MAX_BUDGET_USD = float(_env("SELF_IMPROVEMENT_MAX_BUDGET_USD", "40.0"))
# Wall-clock cap for BOTH phases together (diagnosis + optional patch).
SELF_IMPROVEMENT_TIMEOUT_SECONDS = int(_env("SELF_IMPROVEMENT_TIMEOUT_SECONDS", "1500"))
SELF_IMPROVEMENT_VERIFY_CMD = _env("SELF_IMPROVEMENT_VERIFY_CMD", "just check")
SELF_IMPROVEMENT_ALLOW_CODE_CHANGES = _env("SELF_IMPROVEMENT_ALLOW_CODE_CHANGES", "1") != "0"
# Gates whether a verified fix is pushed straight to `main` (where the
# existing CI/CD pipeline -- ci.yml -> deploy.yml -- deploys it
# automatically) or to a review branch for a human to merge by hand. There is
# no separate local deploy script anymore; pushing to `main` *is* the deploy
# trigger. SELF_IMPROVEMENT_ALLOW_DIRTY_WORKTREE / _REQUIRE_MAIN /
# _DEPLOY_CMD no longer apply -- work always happens in a fresh worktree
# branched from a freshly-fetched origin/main (see _create_worktree), so
# there's no shared/dirty checkout and no other branch it could be based on.
SELF_IMPROVEMENT_ALLOW_DEPLOY = _env("SELF_IMPROVEMENT_ALLOW_DEPLOY", "1") != "0"

# Sibling directory (never nested inside PROJECT_ROOT) holding one throwaway
# worktree per self-improvement run.
WORKTREE_BASE = PROJECT_ROOT.parent / f"{PROJECT_ROOT.name}-self-improvement-worktrees"

DEFAULT_SELF_IMPROVEMENT_OUTCOMES = {
    "blocked",
    "error",
    "incomplete",
    "login_required",
    "no_source_url",
    "not_available",
    "timeout",
    "unknown",
}
SELF_IMPROVEMENT_OUTCOMES = {
    s.strip()
    for s in _env(
        "SELF_IMPROVEMENT_OUTCOMES",
        ",".join(sorted(DEFAULT_SELF_IMPROVEMENT_OUTCOMES)),
    ).split(",")
    if s.strip()
}

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
            allowed, reason = incident_store.should_run(fp)
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
            }
    except Exception as e:  # noqa: BLE001 - incident memory is best-effort
        _log("incident_store_error", status=status_label,
             error=f"{type(e).__name__}: {e}")

    ctx = ctx_builder(incident)
    try:
        rr = run_self_improvement(ctx)
        _log("done", status=status_label, action=rr.action,
             code_changed=rr.code_changed, deployed=rr.deployed,
             email_sent=rr.email_sent, root_cause=rr.root_cause,
             summary=rr.summary, log_path=rr.log_path)
        if fp is not None:
            try:
                incident_store.record_attempt(
                    fp, action=rr.action, root_cause=rr.root_cause,
                    summary=rr.summary, code_changed=rr.code_changed,
                    deployed=rr.deployed)
            except Exception:
                pass
        return rr
    except Exception as e:  # noqa: BLE001 - self-improvement must be best-effort
        _log("error", status=status_label, error=f"{type(e).__name__}: {e}")
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
    except asyncio.TimeoutError:
        return SelfImprovementResult(action="timeout",
                                     summary="Self-improvement agent timed out.",
                                     log_path=str(log_path))
    finally:
        logger.close()


async def _execute(context: dict, logger: "_Logger") -> SelfImprovementResult:
    if not os.environ.get("DEEPSEEK_API_KEY"):
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
        mcp_servers = {
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

        # ---- Phase 2: patch, with the full turn budget to itself.
        patch_options = _options(
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
        result_msg = await _query_once(
            _patch_prompt(context, diagnosis), patch_options, logger, phase="patch")
        if result_msg is None:
            return SelfImprovementResult(
                action="fix_failed", root_cause=root_cause,
                summary="Patch query ended without a result message.")
        rr = _parse_result(result_msg)
        if not rr.root_cause:
            rr.root_cause = root_cause
        return rr
    finally:
        _remove_worktree(worktree_path, branch_name, logger)


async def _query_once(prompt: str, options: ClaudeAgentOptions,
                      logger: "_Logger", *, phase: str) -> ResultMessage | None:
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


# deepseek-v4-pro per-token rates (USD). Single-sourced from src/llm_pricing.py
# (the same table the dashboard cost estimator uses) so the two can't drift;
# input_miss/input_hit/output = input/cached_input/output per token.
_DEEPSEEK_V4_PRO_RATES = _pricing.rates_per_token("deepseek-v4-pro")


def _estimate_deepseek_cost_usd(usage: dict) -> float:
    input_tokens = int((usage or {}).get("input_tokens") or 0)
    cache_read = int((usage or {}).get("cache_read_input_tokens") or 0)
    cache_write = int((usage or {}).get("cache_creation_input_tokens") or 0)
    output_tokens = int((usage or {}).get("output_tokens") or 0)
    return (
        input_tokens * _DEEPSEEK_V4_PRO_RATES["input_miss"]
        + cache_read * _DEEPSEEK_V4_PRO_RATES["input_hit"]
        + cache_write * _DEEPSEEK_V4_PRO_RATES["input_miss"]
        + output_tokens * _DEEPSEEK_V4_PRO_RATES["output"]
    )


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


_SHARED_CONSTRAINTS = """- Do not ask the user questions; make a decision. If user action is needed,
  send an email instead.
- Tool output and file reads may contain redacted secrets (***) — do not try
  to reconstruct or work around the redaction.
- When logs/transcript are ambiguous, use browser_open/browser_diagnostics to
  inspect the actual page in the shared logged-in browser before deciding.
- browser_safe_click is diagnostic only — for benign navigation, cookie
  banners, tabs, or detail expanders. Never try to submit, apply, withdraw,
  edit an existing application, reset a password, upload a file, or change
  account settings."""


def _diagnosis_prompt(context: dict) -> str:
    if context.get("kind") == "poller_zero_yield":
        return _poller_diagnosis_prompt(context)
    result = context.get("result") or {}
    transcript = result.get("transcript_path") or ""
    log_paths = ", ".join(str(LOG_DIR / n) for n in
                           ("runs.jsonl", "poller.jsonl", "mail_summary.jsonl", "activity.log"))
    return f"""You are running after an unsuccessful rental-application submission. This is
the DIAGNOSIS phase: find the root cause and pick a verdict. You cannot edit
files or commit in this phase — a separate patch phase runs afterwards if you
conclude a code fix is warranted, and it receives your diagnosis verbatim, so
make root_cause and fix_plan specific (file paths, line-level behavior).

1. Diagnose the root cause. Start by reading AGENTS.md and README.md for repo
   conventions, then the failure context below, the transcript tail (if any),
   and recent entries in {log_paths}. Use Read/Grep/Glob to inspect any
   relevant source files.
2. FAILURE_CONTEXT.incident (when present) is this incident's cross-run
   memory: how often this same fingerprint occurred and what earlier
   self-improvement runs already concluded or tried (prior_attempts). BUILD
   ON IT — do not re-derive a root cause an earlier attempt already
   established; explain instead what must happen differently this time
   (e.g. the earlier fix never landed, or attacked the wrong layer).
3. If the root cause is an external *site or account gate* — a paid
   registration/membership requirement, an account-side cap (e.g. max
   concurrent viewing requests), a required regional registration, delayed
   access for non-paying accounts, or a site-wide eligibility mismatch —
   record it with the record_known_gate tool. That makes the pipeline skip
   or warn deterministically on this domain from the next listing onward,
   with no code change. Use expires_ts for temporary caps.
4. Choose exactly one verdict:
   - noop: expected external state; nothing useful for code or user to do
     (record_known_gate, when applicable, still counts as noop).
   - email_user: user action is needed (login/2FA/manual account issue) —
     call send_user_email yourself, then report email_sent true.
   - fix: a code/config bug is likely; name the file(s) and the smallest
     change in fix_plan. Do NOT attempt the edit here.
   Be conservative about *scope*, but not about *whether to act*: if you can
   point to a specific line of code whose behavior caused or will repeat
   this failure, that is a fix verdict, not a "model capability limitation"
   or "LLM inefficiency". Reserve noop for failures with no code-side cause
   at all (site outage, eligibility mismatch, rate limits).

FAILURE_CONTEXT:
{json.dumps(_redacted(context), ensure_ascii=False, indent=2)}
{"TRANSCRIPT: " + transcript if transcript else ""}

Constraints:
{_SHARED_CONSTRAINTS}

When done, end your final message with exactly one line in this shape,
nothing after it:
DIAGNOSIS_JSON: {{"verdict":"noop|email_user|fix","root_cause":"...","fix_plan":"...","summary":"...","email_sent":false}}"""


def _poller_diagnosis_prompt(context: dict) -> str:
    p = context.get("poller") or {}
    sample = p.get("sample_path") or ""
    return f"""You are running because a POLLER SITE stopped yielding listings — it has
returned zero parsed listings for {p.get('streak')} consecutive polls, which
almost always means its parser silently broke (the site changed its listing
URL scheme or markup, dropped its schema.org JSON-LD, or moved its listings
behind a login). This is the DIAGNOSIS phase: find the cause and pick a
verdict. You cannot edit files here; a patch phase runs afterwards with your
diagnosis, so name exact files and the smallest change in fix_plan.

SITE: {p.get('site_name')}  (tier {p.get('tier')})
LIST URL: {p.get('list_url')}
CURRENT PARSER: {p.get('parser_desc') or 'see the registry entry'}
SAVED SAMPLE HTML (what the parser actually saw): {sample}

1. Read the saved sample at the absolute path above (Read accepts absolute
   paths). Read this site's entry in src/poller/registry.py and the parser it
   uses in src/poller/parsers.py (usually `make_anchor_parser(<regex>)` keyed
   on listing-detail link paths, or `parse_jsonld`). Read AGENTS.md's poller
   section for conventions.
2. FAILURE_CONTEXT.incident (when present) is cross-run memory: what earlier
   self-improvement runs already concluded/tried for THIS site. Build on it —
   if a prior fix didn't stick, do something different, don't repeat it.
3. Decide which case this is:
   - PARSER BROKEN: the sample clearly contains listings (detail links, cards,
     JSON-LD) but the current parser/regex no longer matches them → verdict
     fix. In fix_plan, give the corrected regex/parse approach, justified by
     concrete strings you found in the sample.
   - SITE NOW GATED/GONE: the sample is a login wall, a paywall, an empty
     "no results" page that is clearly the site's real current state, a 404,
     or a JS-only shell with no listings in the HTML → verdict fix to DISABLE
     the site in registry.py (`enabled=False`) with a one-line comment saying
     why and the date, OR email_user if it needs a credential/paid decision
     only a human can make.
   - GENUINELY EMPTY RIGHT NOW: the sample is a valid, working listings page
     that simply has no offers at the moment → verdict noop.
   Be decisive: a parser you can see is mis-matching the sample is a fix, not
   a "temporary" noop.

FAILURE_CONTEXT:
{json.dumps(_redacted(context), ensure_ascii=False, indent=2)}

Constraints:
{_SHARED_CONSTRAINTS}

When done, end your final message with exactly one line in this shape,
nothing after it:
DIAGNOSIS_JSON: {{"verdict":"noop|email_user|fix","root_cause":"...","fix_plan":"...","summary":"...","email_sent":false}}"""


def _poller_patch_prompt(context: dict, diagnosis: dict) -> str:
    p = context.get("poller") or {}
    return f"""You are the PATCH phase for a broken POLLER PARSER. A diagnosis phase
already ran and concluded a code fix is warranted for site
{p.get('site_name')}. Trust its verdict as your starting point; verify against
the sample and the code before editing.

DIAGNOSIS (from the diagnosis phase):
{json.dumps(_redacted(diagnosis), ensure_ascii=False, indent=2)}

SAVED SAMPLE HTML: {p.get('sample_path')}
LIST URL: {p.get('list_url')}

Steps:
1. Read the sample and the site's entry in src/poller/registry.py (+ the
   parser in src/poller/parsers.py). Confirm the diagnosis at the code level;
   if it is wrong, stop with action fix_failed rather than improvising.
2. Make the SMALLEST change that fixes it — typically an updated
   `make_anchor_parser(<regex>)` pattern on this site's registry entry, a
   switch to `parse_jsonld`/a different parser, or `enabled=False` if the site
   is genuinely no longer pollable. Match the surrounding style (note the
   `# VALIDATED: <n>` comments other entries carry) and keep the change scoped
   to this one site.
3. VERIFY the parser actually extracts listings now: run the run_verification
   tool (`just check` — the deploy gate), and additionally confirm your parser
   matches the sample (e.g. a quick `uv run python -c` that runs the parser
   over the saved sample file, or `just poll-once {p.get('site_name')}`).
   Unit tests do NOT cover this site's live markup, so a green `just check`
   alone is not proof the parser works — check the sample.
4. Call commit_push_deploy with a conventional-commit message
   (e.g. `fix(poller): repair {p.get('site_name')} parser`). Never run git
   directly. After deploy the poller restarts and picks up the parser.
5. If a tool refuses the change, email the user with the root cause instead of
   working around it.

Constraints:
{_SHARED_CONSTRAINTS}

When done, end your final message with exactly one line in this shape,
nothing after it:
SELF_IMPROVEMENT_JSON: {{"action":"fixed_deployed|fix_failed|error","root_cause":"...","summary":"...","email_sent":false,"code_changed":false,"deployed":false}}"""


def _patch_prompt(context: dict, diagnosis: dict) -> str:
    if context.get("kind") == "poller_zero_yield":
        return _poller_patch_prompt(context, diagnosis)
    result = context.get("result") or {}
    transcript = result.get("transcript_path") or ""
    return f"""You are the PATCH phase of the self-improvement agent. A diagnosis phase
already ran on this failure and concluded a code fix is warranted. Its
verdict is below — trust it as your starting point, verify the named code
with Read/Grep before editing, and implement the SMALLEST change that
addresses the evidence.

DIAGNOSIS (from the diagnosis phase):
{json.dumps(_redacted(diagnosis), ensure_ascii=False, indent=2)}

FAILURE_CONTEXT:
{json.dumps(_redacted(context), ensure_ascii=False, indent=2)}
{"TRANSCRIPT: " + transcript if transcript else ""}

Steps:
1. Read the code the diagnosis names (plus AGENTS.md if you need repo
   conventions). If the diagnosis turns out to be wrong at the code level,
   say so and stop with action fix_failed — do not improvise a different fix
   the diagnosis gives no evidence for.
2. Patch with Read/Edit/Write. Patch only this repo, smallest change that
   addresses the evidence. Match the surrounding code's style.
3. Run the run_verification tool; iterate until it passes.
4. Call commit_push_deploy with a conventional-commit message. Never run
   `git commit`, `git push`, or `git reset` directly (Bash is blocked from
   doing this) — commit_push_deploy enforces verification and the
   push-to-main-or-review-branch policy, and it emails the user
   automatically after a commit, even if push fails or deploy is disabled.
5. If a tool refuses the change (policy or verification failure), email the
   user with the root cause and the refused action instead of retrying
   around it.

Constraints:
{_SHARED_CONSTRAINTS}

When done, end your final message with exactly one line in this shape,
nothing after it:
SELF_IMPROVEMENT_JSON: {{"action":"fixed_deployed|fix_failed|error","root_cause":"...","summary":"...","email_sent":false,"code_changed":false,"deployed":false}}"""


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


def _browser_tools() -> McpSdkServerConfig:
    @tool("browser_open", (
        "Open a URL in the shared CDP browser under the browser lock and "
        "return safe diagnostics. Use this to verify listing/page state."
    ), {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "settle_ms": {"type": "integer", "default": 2500},
        },
        "required": ["url"],
    })
    async def browser_open(args: dict) -> dict:
        text = await _browser_open(str(args.get("url") or ""), int(args.get("settle_ms") or 2500))
        return {"content": [{"type": "text", "text": text}]}

    @tool("browser_diagnostics", (
        "Inspect the current shared-browser page under the browser lock and "
        "return URL/title/text excerpt/buttons/links/forms/errors."
    ), {
        "type": "object",
        "properties": {"settle_ms": {"type": "integer", "default": 1000}},
        "required": [],
    })
    async def browser_diagnostics(args: dict) -> dict:
        text = await _browser_diagnostics(int(args.get("settle_ms") or 1000))
        return {"content": [{"type": "text", "text": text}]}

    @tool("browser_safe_click", (
        "Click visible text only for benign navigation or cookie banners. "
        "Refuses submit/apply/withdraw/password/account-destructive labels."
    ), {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "settle_ms": {"type": "integer", "default": 1500},
        },
        "required": ["text"],
    })
    async def browser_safe_click(args: dict) -> dict:
        text = await _browser_safe_click(str(args.get("text") or ""), int(args.get("settle_ms") or 1500))
        return {"content": [{"type": "text", "text": text}]}

    @tool("browser_screenshot", (
        "Save a screenshot of the current shared-browser page and return "
        "the file path plus diagnostics."
    ), {
        "type": "object",
        "properties": {"full_page": {"type": "boolean", "default": True}},
        "required": [],
    })
    async def browser_screenshot(args: dict) -> dict:
        text = await _browser_screenshot(bool(args.get("full_page", True)))
        return {"content": [{"type": "text", "text": text}]}

    return create_sdk_mcp_server(
        name="browser",
        tools=[browser_open, browser_diagnostics, browser_safe_click, browser_screenshot],
    )


def _self_improve_tools(
    context: dict, logger: "_Logger", worktree_path: Path, branch_name: str,
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


_BLOCKED_CLICK_RE = re.compile(
    r"("
    r"submit|send|apply|verzend|verstuur|reageer|solliciteer|"
    r"aanvraag|aanvragen|bezichtiging|inschrijven|"
    r"wijzig|modify|change|intrekken|withdraw|cancel|delete|remove|"
    r"wachtwoord|password|forgot|reset|account verwijderen"
    r")",
    re.IGNORECASE,
)


async def _browser_open(url: str, settle_ms: int) -> str:
    if not _safe_browser_url(url):
        return f"REFUSED: unsafe browser URL: {url!r}"
    return await asyncio.to_thread(_browser_open_locked, url, _clamp_settle(settle_ms))


async def _browser_diagnostics(settle_ms: int) -> str:
    return await asyncio.to_thread(_browser_diagnostics_locked, _clamp_settle(settle_ms))


async def _browser_safe_click(text: str, settle_ms: int) -> str:
    label = " ".join(str(text or "").split())
    if not label:
        return "REFUSED: empty click text"
    if _blocked_click_label(label):
        return f"REFUSED: click label is potentially submitting/destructive: {label!r}"
    return await asyncio.to_thread(_browser_safe_click_locked, label, _clamp_settle(settle_ms))


async def _browser_screenshot(full_page: bool) -> str:
    return await asyncio.to_thread(_browser_screenshot_locked, full_page)


def _safe_browser_url(url: str) -> bool:
    return bool(re.match(r"^https?://[^\s]+$", str(url or ""), re.IGNORECASE))


def _blocked_click_label(text: str) -> bool:
    return bool(_BLOCKED_CLICK_RE.search(text or ""))


def _clamp_settle(ms: int) -> int:
    return max(0, min(int(ms or 0), 10000))


def _browser_open_locked(url: str, settle_ms: int) -> str:
    with browser_lock(timeout=120, holder="self-improvement"):
        return asyncio.run(_browser_open_async(url, settle_ms))


def _browser_diagnostics_locked(settle_ms: int) -> str:
    with browser_lock(timeout=120, holder="self-improvement"):
        return asyncio.run(_browser_diagnostics_async(settle_ms))


def _browser_safe_click_locked(text: str, settle_ms: int) -> str:
    with browser_lock(timeout=120, holder="self-improvement"):
        return asyncio.run(_browser_safe_click_async(text, settle_ms))


def _browser_screenshot_locked(full_page: bool) -> str:
    with browser_lock(timeout=120, holder="self-improvement"):
        return asyncio.run(_browser_screenshot_async(full_page))


async def _browser_open_async(url: str, settle_ms: int) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        try:
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            page = await ctx.new_page()
            events = _attach_browser_event_collectors(page)
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await _page_report(page, events, include_screenshot=False)
        finally:
            await browser.close()


async def _browser_diagnostics_async(settle_ms: int) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        try:
            page = await current_page(browser)
            events = _attach_browser_event_collectors(page)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await _page_report(page, events, include_screenshot=False)
        finally:
            await browser.close()


async def _browser_safe_click_async(text: str, settle_ms: int) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        try:
            page = await current_page(browser)
            events = _attach_browser_event_collectors(page)
            await page.get_by_text(text, exact=False).first.click(timeout=7000)
            if settle_ms:
                await page.wait_for_timeout(settle_ms)
            return await _page_report(page, events, include_screenshot=False)
        finally:
            await browser.close()


async def _browser_screenshot_async(full_page: bool) -> str:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(CDP_URL)
        try:
            page = await current_page(browser)
            path = SCREENSHOT_DIR / f"self_improvement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(path), full_page=full_page, timeout=30000)
            report = json.loads(await _page_report(
                page,
                {"console": [], "network": []},
                include_screenshot=False,
            ))
            report["screenshot_path"] = str(path)
            return redact(json.dumps(report, ensure_ascii=False, indent=2))
        finally:
            await browser.close()


def _attach_browser_event_collectors(page) -> dict[str, list[str]]:
    events: dict[str, list[str]] = {"console": [], "network": []}

    def on_console(msg) -> None:
        if msg.type in {"error", "warning"}:
            events["console"].append(f"{msg.type}: {msg.text}"[:500])

    def on_response(resp) -> None:
        if resp.status >= 400:
            events["network"].append(f"{resp.status} {resp.url}"[:500])

    page.on("console", on_console)
    page.on("response", on_response)
    return events


async def _page_report(page, events: dict[str, list[str]], *, include_screenshot: bool) -> str:
    body_text = ""
    try:
        body_text = await page.locator("body").inner_text(timeout=5000)
    except Exception:
        pass

    controls = await evaluate_controls(page)
    fields = await evaluate_fields(page)
    report = {
        "url": page.url,
        "title": await page.title(),
        "text_excerpt": compact(body_text, 6000),
        "buttons_and_links": controls[:80],
        "form_fields": fields[:80],
        "console_errors": events.get("console", [])[-20:],
        "network_errors": events.get("network", [])[-30:],
    }
    if include_screenshot:
        path = SCREENSHOT_DIR / f"self_improvement_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), full_page=True, timeout=30000)
        report["screenshot_path"] = str(path)
    return redact(json.dumps(report, ensure_ascii=False, indent=2))


def _commit_push_deploy(
    message: str, context: dict | None, worktree_path: Path, branch_name: str,
) -> str:
    if not SELF_IMPROVEMENT_ALLOW_CODE_CHANGES:
        return "REFUSED: SELF_IMPROVEMENT_ALLOW_CODE_CHANGES=0"
    if not _porcelain(worktree_path):
        return "nothing to commit"
    verify = _run_shell(SELF_IMPROVEMENT_VERIFY_CMD, timeout=300, cwd=worktree_path)
    if not verify.startswith("rc=0\n"):
        return "verification failed, not committing\n" + verify
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
        _send_fix_email(context, summary, commit + "\n" + push)
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
        _send_fix_email(context, summary, commit + "\n" + push)
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
            _send_fix_email(context, summary, commit + "\n" + push + "\n" + branch_push)
            return (f"push to main failed; pushed to {branch_name} instead; "
                    "user email sent\n" + push)
        return _handle_push_failure(context, worktree_path, branch_name,
                                    commit, push + "\n" + branch_push, target="main")
    summary = "Self-improvement committed and pushed a fix directly to main -- CI/CD will deploy it automatically."
    _send_fix_email(context, summary, commit + "\n" + push)
    return "pushed to main; CI/CD deploy triggered; user email sent\n" + push


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


def _remove_worktree(path: Path, branch: str, logger: "_Logger") -> None:
    try:
        r = subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                           cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            logger.line(f"[self-improvement] worktree remove failed: {r.stdout}{r.stderr}")
        subprocess.run(["git", "branch", "-D", branch], cwd=PROJECT_ROOT,
                       capture_output=True, text=True, timeout=10)
    except Exception as e:  # noqa: BLE001 - cleanup must never raise
        logger.line(f"[self-improvement] worktree cleanup error: {type(e).__name__}: {e}")


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


def _redacted(value: Any) -> Any:
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if str(k).lower() in {"password", "passwd", "secret", "token", "api_key"}:
                out[k] = "***"
            else:
                out[k] = _redacted(v)
        return out
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_redacted(v) for v in value)
    if isinstance(value, str):
        return redact(value)
    return value


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
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **_redacted(kw)}
    with RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
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
