"""Autonomous diagnosis and self-improvement for failed apply attempts.

The normal application agent handles the rental site. This module handles the
agent itself after an unsuccessful run: inspect redacted logs, decide whether
the cause is an external/user-action state or a code bug, and then either do
nothing, email the user, or patch + verify + commit + push + deploy.
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

from openai import AsyncOpenAI

from .browser_agent import DEEPSEEK_BASE_URL, AgentResult
from .config import CDP_URL, LOG_DIR, PROJECT_ROOT, SCREENSHOT_DIR
from .dashboard.data import redact
from .notify import send_alert
from .poller.browser_lock import browser_lock


def _env(name: str, default: str) -> str:
    """Read SELF_IMPROVEMENT_* env vars, with RECOVERY_* as compatibility aliases."""
    legacy = "RECOVERY_" + name.removeprefix("SELF_IMPROVEMENT_")
    return os.environ.get(name, os.environ.get(legacy, default))


SELF_IMPROVEMENT_ENABLED = _env("SELF_IMPROVEMENT_ENABLED", "1") != "0"
SELF_IMPROVEMENT_MODEL = _env("SELF_IMPROVEMENT_MODEL", os.environ.get("APPLY_MODEL", "deepseek-v4-pro"))
SELF_IMPROVEMENT_MAX_TURNS = int(_env("SELF_IMPROVEMENT_MAX_TURNS", "18"))
SELF_IMPROVEMENT_MAX_TOKENS = int(_env("SELF_IMPROVEMENT_MAX_TOKENS", "12000"))
SELF_IMPROVEMENT_TIMEOUT_SECONDS = int(_env("SELF_IMPROVEMENT_TIMEOUT_SECONDS", "900"))
SELF_IMPROVEMENT_VERIFY_CMD = _env("SELF_IMPROVEMENT_VERIFY_CMD", "just check")
SELF_IMPROVEMENT_DEPLOY_CMD = _env("SELF_IMPROVEMENT_DEPLOY_CMD", "just deploy")
SELF_IMPROVEMENT_ALLOW_CODE_CHANGES = _env("SELF_IMPROVEMENT_ALLOW_CODE_CHANGES", "1") != "0"
SELF_IMPROVEMENT_ALLOW_DEPLOY = _env("SELF_IMPROVEMENT_ALLOW_DEPLOY", "1") != "0"
SELF_IMPROVEMENT_ALLOW_DIRTY_WORKTREE = _env("SELF_IMPROVEMENT_ALLOW_DIRTY_WORKTREE", "0") == "1"
SELF_IMPROVEMENT_REQUIRE_MAIN = _env("SELF_IMPROVEMENT_REQUIRE_MAIN", "1") != "0"

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
TRANSCRIPTS_DIR = LOG_DIR / "transcripts"
_MAX_TOOL_TEXT = 30000


@dataclass
class SelfImprovementResult:
    action: str
    summary: str
    root_cause: str = ""
    email_sent: bool = False
    code_changed: bool = False
    deployed: bool = False


def should_recover(status: str | None) -> bool:
    return SELF_IMPROVEMENT_ENABLED and (status or "") in SELF_IMPROVEMENT_OUTCOMES


def should_improve(status: str | None) -> bool:
    return should_recover(status)


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
    ctx = {
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
    }
    try:
        rr = run_self_improvement(ctx)
        _log("done", status=result.outcome, action=rr.action,
             code_changed=rr.code_changed, deployed=rr.deployed,
             email_sent=rr.email_sent, root_cause=rr.root_cause,
             summary=rr.summary)
        return rr
    except Exception as e:  # noqa: BLE001 - self-improvement must be best-effort
        _log("error", status=result.outcome, error=f"{type(e).__name__}: {e}")
        try:
            send_alert(
                "⚠️ Self-improvement agent failed",
                f"The self-improvement agent crashed while handling {result.outcome}.\n\n"
                f"{type(e).__name__}: {e}\n\n"
                f"Listing: {listing.get('source_url') or '-'}",
            )
        except Exception:
            pass
        return SelfImprovementResult(action="error", summary=f"{type(e).__name__}: {e}")


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
        logger.line(f"[self-improvement] model={SELF_IMPROVEMENT_MODEL} status={context['result']['outcome']}")
        rr = asyncio.run(asyncio.wait_for(
            _run(context, logger),
            timeout=SELF_IMPROVEMENT_TIMEOUT_SECONDS,
        ))
        return rr
    except asyncio.TimeoutError:
        return SelfImprovementResult(action="timeout", summary="Self-improvement agent timed out.")
    finally:
        logger.close()


async def _run(context: dict, logger: "_Logger") -> SelfImprovementResult:
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        logger.line("[self-improvement] DEEPSEEK_API_KEY not set; emailing user")
        send_alert(
            "⚠️ Rental self-improvement needs configuration",
            "The self-improvement agent could not run because DEEPSEEK_API_KEY is not set.",
        )
        return SelfImprovementResult(
            action="emailed_user",
            summary="DEEPSEEK_API_KEY is missing.",
            root_cause="missing_api_key",
            email_sent=True,
        )

    client = AsyncOpenAI(base_url=DEEPSEEK_BASE_URL, api_key=api_key)
    messages: list[dict[str, Any]] = [{"role": "user", "content": _prompt(context)}]
    tools = _tools()

    for turn in range(1, SELF_IMPROVEMENT_MAX_TURNS + 1):
        resp = await client.chat.completions.create(
            model=SELF_IMPROVEMENT_MODEL,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            max_tokens=SELF_IMPROVEMENT_MAX_TOKENS,
            extra_body={"thinking": {"type": "disabled"}},
        )
        choice = resp.choices[0]
        msg = choice.message
        calls = msg.tool_calls or []
        logger.line(f"[self-improvement] turn {turn} finish={choice.finish_reason} calls={len(calls)}")
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            **({"tool_calls": [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in calls
            ]} if calls else {}),
        })
        if msg.content:
            logger.line(f"[self-improvement] say: {msg.content.strip()[:500]}")
        if not calls:
            parsed = _parse_final(msg.content or "")
            if parsed:
                return parsed
            messages.append({
                "role": "user",
                "content": "Return only the required SELF_IMPROVEMENT_JSON object, or call another tool.",
            })
            continue

        for tc in calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            logger.line(f"[self-improvement] call {tc.function.name} {_safe_args(args)}")
            text = await _call_tool(tc.function.name, args, context, logger)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": text[:_MAX_TOOL_TEXT],
            })

    return SelfImprovementResult(action="incomplete", summary="Hit self-improvement turn budget.")


def _prompt(context: dict) -> str:
    return f"""You are the self-improvement agent for a rental application bot.

You are running after an unsuccessful submission. Your job is:
1. Diagnose the root cause from the failure summary, redacted transcript, logs,
   and code.
2. Choose exactly one action:
   - noop: the failure is an expected external state and no user action is useful.
   - email_user: user action is needed, for example login/2FA/manual account issue.
   - fix: a code/config bug is likely; patch it, run verification, commit, push,
     and deploy using the provided tools.
3. Be conservative about code changes. Patch only this repo and only when the
   evidence points to a real bug that caused or will repeat this failure.

Repository architecture and conventions (AGENTS.md):
{_repo_doc("AGENTS.md")}

Repository overview (README.md):
{_repo_doc("README.md")}

Initial failure context:
{json.dumps(_redacted(context), ensure_ascii=False, indent=2)}

Important constraints:
- Do not ask the user questions. If user action is needed, send an email.
- Do not expose passwords or personal documents. Tool output is redacted.
- Use read_context first. Read code/logs as needed.
- When logs/transcript are ambiguous, use browser_open/browser_diagnostics to
  inspect the actual page in the shared logged-in browser before deciding.
- Browser clicks are diagnostic only. Use browser_safe_click only for benign
  navigation, cookie banners, tabs, or detail expanders. Never try to submit,
  apply, withdraw, edit an existing application, reset passwords, upload files,
  or change account settings from self-improvement.
- For a code fix, apply the smallest patch, run verification, then call
  commit_push_deploy.
- Any repo fix/improvement MUST notify the user. The commit_push_deploy tool
  sends that email automatically after a commit, even when push/deploy fails or
  deploy is disabled.
- If tools refuse code changes because policy/env/worktree blocks them, email
  the user with the root cause and the refused action.

When done, output only:
SELF_IMPROVEMENT_JSON: {{"action":"noop|emailed_user|fixed_deployed|fix_failed|error","root_cause":"...","summary":"...","email_sent":false,"code_changed":false,"deployed":false}}
"""


def _repo_doc(name: str) -> str:
    p = PROJECT_ROOT / name
    if not p.exists():
        return f"({name} not found)"
    return redact(p.read_text(encoding="utf-8", errors="replace"))


def _tools() -> list[dict]:
    return [
        _tool("read_context", "Return redacted failure context, transcript tail, recent logs, git state.", {}),
        _tool("read_file", "Read a redacted repository file.", {
            "path": {"type": "string"},
            "max_bytes": {"type": "integer", "default": 20000},
        }, ["path"]),
        _tool("search_code", "Search repository text with ripgrep.", {
            "pattern": {"type": "string"},
            "path": {"type": "string", "default": "."},
        }, ["pattern"]),
        _tool("browser_open", (
            "Open a URL in the shared CDP browser under the browser lock and "
            "return safe diagnostics. Use this to verify listing/page state."
        ), {
            "url": {"type": "string"},
            "settle_ms": {"type": "integer", "default": 2500},
        }, ["url"]),
        _tool("browser_diagnostics", (
            "Inspect the current shared-browser page under the browser lock and "
            "return URL/title/text excerpt/buttons/links/forms/errors."
        ), {
            "settle_ms": {"type": "integer", "default": 1000},
        }),
        _tool("browser_safe_click", (
            "Click visible text only for benign navigation or cookie banners. "
            "Refuses submit/apply/withdraw/password/account-destructive labels."
        ), {
            "text": {"type": "string"},
            "settle_ms": {"type": "integer", "default": 1500},
        }, ["text"]),
        _tool("browser_screenshot", (
            "Save a screenshot of the current shared-browser page and return "
            "the file path plus diagnostics."
        ), {
            "full_page": {"type": "boolean", "default": True},
        }),
        _tool("apply_patch", "Apply a unified diff patch to repository files.", {
            "diff": {"type": "string"},
        }, ["diff"]),
        _tool("run_verification", "Run the configured verification command.", {}),
        _tool("git_status", "Return branch and porcelain git status.", {}),
        _tool("git_diff", "Return current git diff.", {}),
        _tool("commit_push_deploy", "Commit current changes, push, and deploy.", {
            "message": {"type": "string"},
        }, ["message"]),
        _tool("send_user_email", "Email the configured recipient about required user action.", {
            "subject": {"type": "string"},
            "body": {"type": "string"},
        }, ["subject", "body"]),
    ]


def _tool(name: str, description: str, props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required or [],
            },
        },
    }


async def _call_tool(name: str, args: dict, context: dict, logger: "_Logger") -> str:
    try:
        if name == "read_context":
            return _read_context(context)
        if name == "read_file":
            return _read_file(args.get("path", ""), int(args.get("max_bytes") or 20000))
        if name == "search_code":
            return _search_code(args.get("pattern", ""), args.get("path") or ".")
        if name == "browser_open":
            return await _browser_open(args.get("url", ""), int(args.get("settle_ms") or 2500))
        if name == "browser_diagnostics":
            return await _browser_diagnostics(int(args.get("settle_ms") or 1000))
        if name == "browser_safe_click":
            return await _browser_safe_click(
                args.get("text", ""),
                int(args.get("settle_ms") or 1500),
            )
        if name == "browser_screenshot":
            return await _browser_screenshot(bool(args.get("full_page", True)))
        if name == "apply_patch":
            return _apply_diff(args.get("diff", ""))
        if name == "run_verification":
            return _run_shell(SELF_IMPROVEMENT_VERIFY_CMD, timeout=300)
        if name == "git_status":
            return _git_status()
        if name == "git_diff":
            return _run(["git", "diff"], timeout=30)
        if name == "commit_push_deploy":
            return _commit_push_deploy(
                args.get("message", "fix(self-improvement): repair apply failure"),
                context,
            )
        if name == "send_user_email":
            send_alert(str(args.get("subject") or "Rental agent needs attention"),
                       redact(str(args.get("body") or "")))
            return "email sent"
    except Exception as e:  # noqa: BLE001
        logger.line(f"[self-improvement] tool error: {type(e).__name__}: {e}")
        return f"TOOL_ERROR {type(e).__name__}: {e}"
    return f"unknown tool: {name}"


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
    with browser_lock(timeout=1800):
        return asyncio.run(_browser_open_async(url, settle_ms))


def _browser_diagnostics_locked(settle_ms: int) -> str:
    with browser_lock(timeout=1800):
        return asyncio.run(_browser_diagnostics_async(settle_ms))


def _browser_safe_click_locked(text: str, settle_ms: int) -> str:
    with browser_lock(timeout=1800):
        return asyncio.run(_browser_safe_click_async(text, settle_ms))


def _browser_screenshot_locked(full_page: bool) -> str:
    with browser_lock(timeout=1800):
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
            page = await _current_page(browser)
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
            page = await _current_page(browser)
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
            page = await _current_page(browser)
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


async def _current_page(browser):
    ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
    if ctx.pages:
        page = ctx.pages[-1]
    else:
        page = await ctx.new_page()
    return page


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

    controls = await _evaluate_controls(page)
    fields = await _evaluate_fields(page)
    report = {
        "url": page.url,
        "title": await page.title(),
        "text_excerpt": _compact(body_text, 6000),
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


async def _evaluate_controls(page) -> list[dict]:
    script = """
    els => els.map(el => {
      const text = (el.innerText || el.value || el.getAttribute('aria-label') ||
        el.getAttribute('title') || '').replace(/\\s+/g, ' ').trim();
      const href = el.href || el.getAttribute('href') || '';
      return {
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        text: text.slice(0, 160),
        href: href.slice(0, 240),
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
      };
    }).filter(x => x.text || x.href)
    """
    try:
        return await page.locator(
            "a, button, input[type=button], input[type=submit], [role=button]"
        ).evaluate_all(script)
    except Exception:
        return []


async def _evaluate_fields(page) -> list[dict]:
    script = """
    els => els.map(el => {
      const id = el.id || '';
      const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
      const text = (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
        (label && label.innerText) || el.getAttribute('name') || id || '')
        .replace(/\\s+/g, ' ').trim();
      return {
        tag: el.tagName.toLowerCase(),
        type: el.getAttribute('type') || '',
        label: text.slice(0, 160),
        required: !!el.required || el.getAttribute('aria-required') === 'true',
        disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'
      };
    }).filter(x => x.label || x.type)
    """
    try:
        return await page.locator("input, textarea, select").evaluate_all(script)
    except Exception:
        return []


def _compact(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + " ...[truncated]"


def _read_context(context: dict) -> str:
    chunks = [
        "FAILURE_CONTEXT\n" + json.dumps(_redacted(context), ensure_ascii=False, indent=2),
        "GIT_STATUS\n" + _git_status(),
    ]
    transcript = context.get("result", {}).get("transcript_path")
    if transcript:
        chunks.append("TRANSCRIPT_TAIL\n" + _tail_file(Path(transcript), 40000))
    for p in (LOG_DIR / "runs.jsonl", LOG_DIR / "poller.jsonl", LOG_DIR / "mail_summary.jsonl", LOG_DIR / "activity.log"):
        chunks.append(f"{p.name.upper()}_TAIL\n" + _tail_file(p, 20000))
    return "\n\n".join(chunks)


def _read_file(path: str, max_bytes: int) -> str:
    p = _safe_repo_path(path)
    if not p.exists() or not p.is_file():
        return f"not a file: {path}"
    return redact(p.read_text(encoding="utf-8", errors="replace")[:max(1000, min(max_bytes, 60000))])


def _search_code(pattern: str, path: str) -> str:
    if not pattern:
        return "empty pattern"
    p = _safe_repo_path(path)
    return _run([
        "rg", "-n",
        "--glob", "!state/**",
        "--glob", "!documents/**",
        "--glob", "!.git/**",
        "--glob", "!.env",
        "--",
        pattern,
        str(p),
    ], timeout=30)


def _apply_diff(diff: str) -> str:
    if not SELF_IMPROVEMENT_ALLOW_CODE_CHANGES:
        return "REFUSED: SELF_IMPROVEMENT_ALLOW_CODE_CHANGES=0"
    if not SELF_IMPROVEMENT_ALLOW_DIRTY_WORKTREE and _porcelain():
        return "REFUSED: worktree is dirty before self-improvement patch"
    if not diff.strip():
        return "empty diff"
    _validate_diff_paths(diff)
    check = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=PROJECT_ROOT,
        input=diff,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if check.returncode != 0:
        return f"git apply --check failed\n{check.stdout}{check.stderr}"
    applied = subprocess.run(
        ["git", "apply", "-"],
        cwd=PROJECT_ROOT,
        input=diff,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return f"rc={applied.returncode}\n{applied.stdout}{applied.stderr}\n{_git_status()}"


def _commit_push_deploy(message: str, context: dict | None = None) -> str:
    if not SELF_IMPROVEMENT_ALLOW_CODE_CHANGES:
        return "REFUSED: SELF_IMPROVEMENT_ALLOW_CODE_CHANGES=0"
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=10).strip()
    if SELF_IMPROVEMENT_REQUIRE_MAIN and branch not in {"main", "master"}:
        return f"REFUSED: current branch is {branch!r}, not main/master"
    if not _porcelain():
        return "nothing to commit"
    verify = _run_shell(SELF_IMPROVEMENT_VERIFY_CMD, timeout=300)
    if not verify.startswith("rc=0\n"):
        return "verification failed, not committing\n" + verify
    add = _run(["git", "add", "-A"], timeout=30)
    commit = _run(["git", "commit", "-m", _commit_message(message)], timeout=60)
    if not commit.startswith("rc=0\n"):
        return "commit failed\n" + add + "\n" + commit
    push = _run(["git", "push"], timeout=120)
    if not push.startswith("rc=0\n"):
        summary = "Self-improvement committed a local repo fix but push failed."
        _send_fix_email(context, summary, commit + "\n" + push)
        return "push failed; user email sent\n" + push
    if not SELF_IMPROVEMENT_ALLOW_DEPLOY:
        summary = "Self-improvement committed and pushed a repo fix; deploy was disabled."
        _send_fix_email(context, summary, commit + "\n" + push)
        return "committed and pushed; deploy skipped because SELF_IMPROVEMENT_ALLOW_DEPLOY=0; user email sent"
    deploy = _run_shell(SELF_IMPROVEMENT_DEPLOY_CMD, timeout=600)
    summary = "Self-improvement committed, pushed, and attempted deploy for a repo fix."
    _send_fix_email(context, summary, commit + "\n" + push + "\n" + deploy)
    return "committed, pushed, deploy attempted; user email sent\n" + deploy


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


def _validate_diff_paths(diff: str) -> None:
    allowed_prefixes = ("src/", "tests/", "deploy/", "docs/", "justfile", "pyproject.toml", "README.md")
    paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("+++ ", "--- ")):
            raw = line[4:].strip()
            if raw == "/dev/null":
                continue
            if raw.startswith(("a/", "b/")):
                raw = raw[2:]
            paths.add(raw)
    if not paths:
        raise ValueError("diff has no file paths")
    for raw in paths:
        if raw.startswith("/") or ".." in Path(raw).parts:
            raise ValueError(f"unsafe diff path: {raw}")
        if not raw.startswith(allowed_prefixes):
            raise ValueError(f"diff path not allowed for self-improvement: {raw}")


def _safe_repo_path(path: str) -> Path:
    raw = Path(path or ".")
    p = raw if raw.is_absolute() else PROJECT_ROOT / raw
    p = p.resolve()
    root = PROJECT_ROOT.resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path outside repo: {path}")
    rel = p.relative_to(root)
    if rel.parts and rel.parts[0] in {"state", "documents", ".git"}:
        raise ValueError(f"refusing sensitive path: {path}")
    if rel.name == ".env" or rel.suffix in {".key", ".pem", ".p12"}:
        raise ValueError(f"refusing sensitive path: {path}")
    return p


def _tail_file(path: Path, max_chars: int) -> str:
    try:
        p = _safe_repo_path(str(path))
        if not p.exists() or not p.is_file():
            return "(missing)"
        text = p.read_text(encoding="utf-8", errors="replace")
        return redact(text[-max_chars:])
    except Exception as e:  # noqa: BLE001
        return f"(unavailable: {type(e).__name__}: {e})"


def _git_status() -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], timeout=10) + _run(
        ["git", "status", "--short"], timeout=10)


def _porcelain() -> str:
    r = subprocess.run(["git", "status", "--porcelain"], cwd=PROJECT_ROOT,
                       capture_output=True, text=True, timeout=10)
    return r.stdout.strip()


def _run(args: list[str], timeout: int) -> str:
    r = subprocess.run(args, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=timeout)
    return redact(f"rc={r.returncode}\n{r.stdout}{r.stderr}")[:_MAX_TOOL_TEXT]


def _run_shell(command: str, timeout: int) -> str:
    r = subprocess.run(command, cwd=PROJECT_ROOT, capture_output=True, text=True,
                       shell=True, timeout=timeout)
    return redact(f"rc={r.returncode}\n{r.stdout}{r.stderr}")[:_MAX_TOOL_TEXT]


def _parse_final(text: str) -> SelfImprovementResult | None:
    m = re.search(r"SELF_IMPROVEMENT_JSON:\s*(\{.*\})", text or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return None
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


def _safe_args(args: dict) -> dict:
    safe = dict(args)
    if "diff" in safe:
        safe["diff"] = f"<diff {len(str(args.get('diff') or ''))} chars>"
    if "body" in safe:
        safe["body"] = redact(str(safe["body"]))[:300]
    return safe


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
