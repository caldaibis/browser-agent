"""The self-improvement agent's phase prompts (diagnosis / patch).
Operational policy lives in this text — keep prompt changes reviewable on
their own, apart from engine code."""
from __future__ import annotations

import json

from ..config import LOG_DIR
from .util import _redacted


_SHARED_CONSTRAINTS = """- Do not ask the user questions; make a decision. If user action is needed,
  send an email instead.
- Repository source paths must be relative to the session worktree. Absolute
  paths under the live checkout are read-only evidence paths, never edit or
  command working directories.
- Tool output and file reads may contain redacted secrets (***) — do not try
  to reconstruct or work around the redaction.
- When logs/transcript are ambiguous, use browser_open/browser_diagnostics to
  inspect the actual page in the shared logged-in browser before deciding.
- browser_safe_click is diagnostic only — for benign navigation, cookie
  banners, tabs, or detail expanders. Never try to submit, apply, withdraw,
  edit an existing application, reset a password, upload a file, or change
  account settings."""

_STOP_RULE = """STOP RULE: once one root cause is supported by direct evidence and you
can state a bounded verdict/fix plan, call submit_diagnosis immediately. Do
not research package histories, alternative implementations, deployment
mechanics, or extra corroboration after the causal error is known. The tool's
record is authoritative even if the SDK later reaches its turn limit."""


def _diagnosis_prompt(context: dict) -> str:
    if context.get("kind") == "session_keeper_adapter":
        return _session_keeper_diagnosis_prompt(context)
    result = context.get("result") or {}
    transcript = result.get("transcript_path") or ""
    log_paths = ", ".join(str(LOG_DIR / n) for n in
                           ("runs.jsonl", "mail_summary.jsonl", "activity.log"))
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
5. {_STOP_RULE}

FAILURE_CONTEXT:
{json.dumps(_redacted(context), ensure_ascii=False, indent=2)}
{"TRANSCRIPT: " + transcript if transcript else ""}

Constraints:
{_SHARED_CONSTRAINTS}

When done, call submit_diagnosis with these fields. The line below is only a
compatibility fallback if the tool is unavailable:
DIAGNOSIS_JSON: {{"verdict":"noop|email_user|fix","root_cause":"...","fix_plan":"...","summary":"...","email_sent":false}}"""


def _session_keeper_diagnosis_prompt(context: dict) -> str:
    sk = context.get("session_keeper") or {}
    return f"""You are running because the SESSION KEEPER's login adapter for a source site
completed a repair attempt but the site still looks logged out afterward.
This trigger ONLY fires for that one case -- CAPTCHA, 2FA, an account-chooser
mismatch, and a rejected stored password are real external gates the adapter
already detects and alerts on directly (see
session_keeper._HUMAN_GATE_OUTCOMES); none of those reach here. This is the
DIAGNOSIS phase: find the cause and pick a verdict. You cannot edit files
here; a patch phase runs afterwards with your diagnosis, so name exact
functions and the smallest change in fix_plan.

DOMAIN: {sk.get('domain')}
PROBE URL (authenticated page the adapter checks after login): {sk.get('probe_url')}
LOGIN URL: {sk.get('login_url')}
WHAT THE ADAPTER OBSERVED: {sk.get('detail')}

1. Read `src/session_keeper.py` in full: the `ADAPTERS` registry entry for
   this domain and its `login` function (e.g. `_login_huurwoningen`) -- the
   exact button-text regexes, form-field selectors, and blocker-detection
   regexes it relies on.
2. FAILURE_CONTEXT.incident (when present) is this incident's cross-run
   memory: what earlier self-improvement runs already concluded/tried for
   THIS domain. Build on it -- don't re-derive a root cause an earlier
   attempt already established.
3. Use browser_open/browser_diagnostics to inspect the LIVE login page at
   LOGIN URL in the shared logged-in browser (the same profile the adapter
   itself uses). Compare what you see against the adapter's assumptions.
4. Choose exactly one verdict:
   - fix: the live page's button text, field selectors, or login flow no
     longer match what the adapter looks for (e.g. the Google SSO button
     text changed, the email/password inputs no longer match the adapter's
     locators, or an extra step was inserted). Name the exact stale
     assumption and the corrected selector/text in fix_plan.
   - noop: live inspection shows the flow actually works fine now (the
     failure was transient -- a one-off timing issue, a momentary site
     hiccup). Nothing to fix.
   - email_user: live inspection reveals a genuine account/security state
     (e.g. the account is locked, suspended, or a security review is
     required) that the adapter's blocker detection didn't recognize as
     such -- a human needs to act, not a code fix. Call send_user_email
     yourself, then report email_sent true.
   Be conservative about *scope*, but not about *whether to act*: if the live
   page contradicts a specific selector/regex in the adapter, that is a fix,
   not a noop.
5. {_STOP_RULE}

FAILURE_CONTEXT:
{json.dumps(_redacted(context), ensure_ascii=False, indent=2)}

Constraints:
{_SHARED_CONSTRAINTS}

When done, call submit_diagnosis with these fields. The line below is only a
compatibility fallback if the tool is unavailable:
DIAGNOSIS_JSON: {{"verdict":"noop|email_user|fix","root_cause":"...","fix_plan":"...","summary":"...","email_sent":false}}"""


def _session_keeper_patch_prompt(context: dict, diagnosis: dict,
                                 candidate_strategy: str = "") -> str:
    sk = context.get("session_keeper") or {}
    return f"""You are the PATCH phase for a stale SESSION KEEPER login adapter. A diagnosis
phase already ran and concluded a code fix is warranted for the adapter
covering {sk.get('domain')}. Trust its verdict as your starting point; verify
against the live login page and the code before editing.

DIAGNOSIS (from the diagnosis phase):
{json.dumps(_redacted(diagnosis), ensure_ascii=False, indent=2)}

CANDIDATE STRATEGY:
{candidate_strategy or "smallest_fix"} — keep the fix bounded to this angle
unless the code proves it impossible. Preserve the adapter's safety rules:
never click a password-reset/forgot-password control, never expose a
credential to an LLM message or a log line, never widen what counts as
"logged in".

LOGIN URL: {sk.get('login_url')}
PROBE URL: {sk.get('probe_url')}

Steps:
1. Read `src/session_keeper.py`'s `ADAPTERS` entry and `login` function for
   this domain. Use browser_open/browser_diagnostics against LOGIN URL to
   confirm the diagnosis at the code level; if it's wrong, stop with action
   fix_failed rather than improvising.
2. Make the SMALLEST change that fixes it -- typically an updated button-text
   regex, form-field selector, or blocker-detection regex in this domain's
   `login` function. Match the surrounding style and keep the change scoped
   to this one adapter; do not alter other domains' entries.
3. Run the run_verification tool (`just check`). Additionally verify the
   adapter actually restores the session against the live logged-in browser:
   `uv run python -m src.session_keeper {sk.get('domain')}` should report
   `outcome=ok` or `outcome=repaired`, not a blocker. Unit tests do not cover
   live site markup, so a green `just check` alone is not proof.
4. Call commit_push_deploy with a conventional-commit message (e.g.
   `fix(session_keeper): repair {sk.get('domain')} login adapter`). Never run
   git directly.
5. If a tool refuses the change, email the user with the root cause instead
   of working around it.
6. commit_push_deploy records the authoritative terminal outcome. After it
   returns, STOP immediately.

Constraints:
{_SHARED_CONSTRAINTS}

When done, end your final message with exactly one line in this shape,
nothing after it:
SELF_IMPROVEMENT_JSON: {{"action":"fixed_deployed|fixed_review|fix_failed|error","root_cause":"...","summary":"...","email_sent":false,"code_changed":false,"deployed":false}}"""


def _patch_prompt(context: dict, diagnosis: dict,
                  candidate_strategy: str = "") -> str:
    if context.get("kind") == "session_keeper_adapter":
        return _session_keeper_patch_prompt(context, diagnosis, candidate_strategy)
    result = context.get("result") or {}
    transcript = result.get("transcript_path") or ""
    return f"""You are the PATCH phase of the self-improvement agent. A diagnosis phase
already ran on this failure and concluded a code fix is warranted. Its
verdict is below — trust it as your starting point, verify the named code
with Read/Grep before editing, and implement the SMALLEST change that
addresses the evidence.

DIAGNOSIS (from the diagnosis phase):
{json.dumps(_redacted(diagnosis), ensure_ascii=False, indent=2)}

CANDIDATE STRATEGY:
{candidate_strategy or "smallest_fix"} — treat this as the proposed surface for
this candidate. If this is a recurrent incident, other candidates may try other
surfaces; keep this candidate narrow and make the validation evidence decide.
Preserve passing behavior: do not weaken rent/eligibility/payment/already-
applied safeguards to make the failing trace look better.

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
6. commit_push_deploy records the authoritative terminal outcome. After it
   returns, STOP immediately — do not inspect git state, restage files, verify
   the pushed commit, or spend another turn narrating the result.

Constraints:
{_SHARED_CONSTRAINTS}

When done, end your final message with exactly one line in this shape,
nothing after it:
SELF_IMPROVEMENT_JSON: {{"action":"fixed_deployed|fixed_review|fix_failed|error","root_cause":"...","summary":"...","email_sent":false,"code_changed":false,"deployed":false}}"""
