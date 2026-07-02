"""Per-domain site playbooks — persistent memory of how each rental site works.

Without this, every apply run rediscovers each site's flow from scratch: where
the real apply button is, which prominent button is a paid upsell, which
dialogs lack ARIA roles, what the login quirk is. The entire Hof van Oslo saga
(60-turn timeouts, a paid-subscription dark pattern, three ref-less dialogs)
was the agent lacking knowledge a human gains in one session — and the only
place such knowledge could accrete was hand-edits to apply.py's prompt.

After every real agent run, `update_after_run` makes one cheap LLM pass over
the redacted transcript per touched domain and rewrites
``state/site_playbooks/<domain>.md`` — durable site mechanics only, no
listing-specific facts, no personal data. `apply.build_prompt` injects the
playbook for the listing's domain into the next run on that site, so lessons
compound: turns, tokens, and latency drop with every run on a known site.

Everything here is best-effort and fail-open: a playbook is a bonus, never a
reason for an apply to fail. `state/` is gitignored, like all runtime state.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .config import PROJECT_ROOT

PLAYBOOK_DIR = PROJECT_ROOT / "state" / "site_playbooks"

# One cheap non-reasoning call per touched domain; reuses the apply key/model.
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
PLAYBOOK_MODEL = os.environ.get(
    "PLAYBOOK_MODEL", os.environ.get("APPLY_MODEL", "deepseek-v4-pro"))
PLAYBOOK_MAX_CHARS = int(os.environ.get("PLAYBOOK_MAX_CHARS", "4000"))
PLAYBOOK_TIMEOUT_SECONDS = int(os.environ.get("PLAYBOOK_TIMEOUT_SECONDS", "120"))

# Transcript tail fed to the distiller. Covers a whole normal run; caps the
# bill on pathological ones.
_TRANSCRIPT_TAIL_CHARS = 60000

_DISTILL_PROMPT = """\
You maintain a per-site "playbook": durable, reusable knowledge about how to \
complete a rental application on ONE specific website: {domain}.

Below are the current playbook for {domain} (may be empty) and the transcript \
of the latest automated apply run that touched this site (final outcome: \
{outcome}).

Rewrite the FULL playbook, merging anything NEW and DURABLE this run revealed \
into what is already there. Durable means it will still be true for the next \
listing on this site: login method and quirks, where the real apply/viewing \
action lives, dialogs or overlays that need the fallback DOM tools, form \
fields and their pitfalls, upload slots, paid-upsell traps to avoid, wording \
that signals "already applied" or "not eligible" on this site.

Hard rules:
- Site mechanics ONLY. No listing-specific facts (addresses, prices, dates),
  no personal data, no usernames, no passwords, no secrets.
- Only what the transcript shows actually happened — no speculation.
- Terse markdown bullets, most important first, under {max_chars} characters.
- If this run revealed nothing new and durable, output the current playbook
  unchanged (or "(nothing known yet)" if it is empty and the run showed
  nothing).

Output ONLY the playbook markdown, no preamble, no code fences.

CURRENT PLAYBOOK for {domain}:
\"\"\"
{current}
\"\"\"

TRANSCRIPT (redacted, tail):
\"\"\"
{transcript}
\"\"\"
"""


def domain_for(url: str) -> str:
    """Lowercased host without a leading www., or "" if unparseable."""
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _path(domain: str) -> Path:
    return PLAYBOOK_DIR / f"{domain}.md"


def load(domain: str) -> str | None:
    """The stored playbook for a domain, or None when absent/empty."""
    if not domain:
        return None
    try:
        text = _path(domain).read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return text[:PLAYBOOK_MAX_CHARS] or None


def load_for_url(url: str) -> tuple[str, str] | None:
    """(domain, playbook) for a listing URL, or None when we know nothing."""
    domain = domain_for(url)
    text = load(domain)
    return (domain, text) if text else None


def update_after_run(listing: dict, result) -> None:
    """Distill durable site knowledge out of a finished apply run.

    Best-effort: catches everything and only prints, because a playbook is a
    bonus — it must never turn a submitted application into an error."""
    try:
        _update(listing, result)
    except Exception as e:  # noqa: BLE001 - fail-open by design, see docstring
        print(f"[playbook] update skipped: {type(e).__name__}: {e}")


def _update(listing: dict, result) -> None:
    # A yielded run was aborted for priority, not finished — nothing to learn.
    if result.outcome == "yielded" or not getattr(result, "transcript_path", ""):
        return
    transcript_path = Path(result.transcript_path)
    if not transcript_path.exists():
        return
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return

    # Same redaction the dashboard/self-improvement agent use: transcripts can
    # contain typed passwords in tool args.
    from .dashboard.data import redact
    transcript = redact(
        transcript_path.read_text(encoding="utf-8")[-_TRANSCRIPT_TAIL_CHARS:])

    # The domain the listing was discovered at AND the real destination the run
    # reached (for aggregators these differ; both flows are worth remembering).
    domains = {domain_for(listing.get("source_url", ""))}
    if getattr(result, "resolved_url", ""):
        domains.add(domain_for(result.resolved_url))
    domains.discard("")
    if not domains:
        return

    from openai import OpenAI
    client = OpenAI(base_url=DEEPSEEK_BASE_URL, api_key=api_key,
                    timeout=PLAYBOOK_TIMEOUT_SECONDS)
    for domain in sorted(domains):
        prompt = _DISTILL_PROMPT.format(
            domain=domain,
            outcome=result.outcome,
            max_chars=PLAYBOOK_MAX_CHARS,
            current=load(domain) or "(empty)",
            transcript=transcript,
        )
        resp = client.chat.completions.create(
            model=PLAYBOOK_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            # Known LiteLLM/DeepSeek pitfall: thinking mode wraps the reply in
            # a fake reasoning block. Distilling bullets needs no reasoning.
            extra_body={"thinking": {"type": "disabled"}},
        )
        new = (resp.choices[0].message.content or "").strip()
        if not new:
            continue
        PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().isoformat(timespec="seconds")
        _path(domain).write_text(
            new[:PLAYBOOK_MAX_CHARS]
            + f"\n\n<!-- updated {stamp} after outcome={result.outcome} -->\n",
            encoding="utf-8")
        print(f"[playbook] updated {domain} ({len(new)} chars)")
