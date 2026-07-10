"""Per-domain site playbooks — persistent memory of how each rental site works.

Without this, every apply run rediscovers each site's flow from scratch: where
the real apply button is, which prominent button is a paid upsell, which
dialogs lack ARIA roles, what the login quirk is. The entire Hof van Oslo saga
(60-turn timeouts, a paid-subscription dark pattern, three ref-less dialogs)
was the agent lacking knowledge a human gains in one session — and the only
place such knowledge could accrete was hand-edits to apply.py's prompt.

After every real agent run, `update_after_run` makes one cheap LLM pass over
the redacted transcript per touched domain and asks for itemized durable
lessons. Those lessons are merged deterministically into
``state/site_playbooks/<domain>.json`` and rendered back to
``state/site_playbooks/<domain>.md`` for prompt injection. This follows the
ACE-style "grow and refine" pattern: append localized deltas, update counters,
and only render the current top lessons instead of asking an LLM to rewrite a
single prompt blob on every run.

Everything here is best-effort and fail-open: a playbook is a bonus, never a
reason for an apply to fail. `state/` is gitignored, like all runtime state.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from .config import PROJECT_ROOT
from .settings import settings
from .eventlog import get_logger

_LOG = get_logger("playbooks")
from .eventlog import utc_now_iso

PLAYBOOK_DIR = PROJECT_ROOT / "state" / "site_playbooks"

# One cheap non-reasoning call per touched domain; reuses the apply key/model.
DEEPSEEK_BASE_URL = settings().deepseek_base_url
PLAYBOOK_MODEL = settings().playbook_model
PLAYBOOK_MAX_CHARS = settings().playbook_max_chars
PLAYBOOK_TIMEOUT_SECONDS = settings().playbook_timeout_seconds
PLAYBOOK_MAX_ITEMS = settings().playbook_max_items

# Transcript tail fed to the distiller. Covers a whole normal run; caps the
# bill on pathological ones.
_TRANSCRIPT_TAIL_CHARS = 60000

_DISTILL_PROMPT = """\
You maintain a per-site "playbook": durable, reusable knowledge about how to \
complete a rental application on ONE specific website: {domain}.

Below are the current itemized playbook for {domain} (may be empty) and the \
transcript of the latest automated apply run that touched this site (final \
outcome: {outcome}).

Extract only NEW durable lessons this run revealed. Durable means it will \
still be true for the next listing on this site: login method and quirks, \
where the real apply/viewing action lives, dialogs or overlays that need the \
fallback DOM tools, form fields and their pitfalls, upload slots, paid-upsell \
traps to avoid, wording that signals "already applied" or "not eligible" on \
this site.

Hard rules:
- Site mechanics ONLY. No listing-specific facts (addresses, prices, dates),
  no personal data, no usernames, no passwords, no secrets.
- Only what the transcript shows actually happened — no speculation.
- Terse, concrete lessons. One durable idea per item.
- If this run revealed nothing new and durable, output {{"items":[]}}.

Output ONLY compact JSON, no preamble, no code fences:
{{"items":[{{"id":"short.stable.slug","surface":"login|apply_action|dialog|form|upload|upsell|already_applied|eligibility|other","content":"durable lesson"}}]}}

CURRENT ITEMIZED PLAYBOOK for {domain}:
\"\"\"
{current}
\"\"\"

TRANSCRIPT (redacted, tail):
\"\"\"
{transcript}
\"\"\"
"""


@dataclass
class PlaybookItem:
    id: str
    surface: str
    content: str
    helpful: int = 0
    harmful: int = 0
    first_seen: str = ""
    last_seen: str = ""
    evidence_count: int = 0


def domain_for(url: str) -> str:
    """Lowercased host without a leading www., or "" if unparseable."""
    host = (urlparse((url or "").strip()).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _path(domain: str) -> Path:
    return PLAYBOOK_DIR / f"{domain}.md"


def _json_path(domain: str) -> Path:
    return PLAYBOOK_DIR / f"{domain}.json"


def load(domain: str) -> str | None:
    """The stored playbook for a domain, or None when absent/empty."""
    if not domain:
        return None
    items = _load_items(domain)
    if items:
        return _render_items(items)[:PLAYBOOK_MAX_CHARS] or None
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


def items_for_domain(domain: str) -> list[dict[str, Any]]:
    """Structured playbook items, newest counters included, for dashboards/tests."""
    return [asdict(item) for item in _load_items(domain)]


def update_after_run(listing: dict, result) -> None:
    """Distill durable site knowledge out of a finished apply run.

    Best-effort: catches everything and only prints, because a playbook is a
    bonus — it must never turn a submitted application into an error."""
    try:
        _update(listing, result)
    except Exception as e:  # noqa: BLE001 - fail-open by design, see docstring
        _LOG.info(f"update skipped: {type(e).__name__}: {e}")


def _update(listing: dict, result) -> None:
    # A yielded run was aborted for priority, not finished — nothing to learn.
    if result.outcome == "yielded" or not getattr(result, "transcript_path", ""):
        return
    transcript_path = Path(result.transcript_path)
    if not transcript_path.exists():
        return
    api_key = settings().deepseek_api_key
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
        current_items = _load_items(domain)
        prompt = _DISTILL_PROMPT.format(
            domain=domain,
            outcome=result.outcome,
            max_chars=PLAYBOOK_MAX_CHARS,
            current=json.dumps([asdict(i) for i in current_items],
                               ensure_ascii=False, indent=2) or "[]",
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
        merged = _merge_items(current_items, _parse_delta_items(new),
                              outcome=result.outcome)
        if merged:
            _write_items(domain, merged, result.outcome)
        else:
            # Backward-compatible fallback for older tests/manual calls where
            # the distiller returned markdown instead of JSON.
            fallback = _markdown_delta_items(new)
            if fallback:
                _write_items(domain, _merge_items(current_items, fallback,
                                                  outcome=result.outcome),
                             result.outcome)
                continue
        PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
        stamp = utc_now_iso()
        if not merged and current_items:
            _path(domain).write_text(
                _render_items(current_items)[:PLAYBOOK_MAX_CHARS]
                + f"\n\n<!-- checked {stamp} after outcome={result.outcome} -->\n",
                encoding="utf-8")
        _LOG.info(f"updated {domain} ({len(load(domain) or '')} chars)")


def _load_items(domain: str) -> list[PlaybookItem]:
    try:
        raw = json.loads(_json_path(domain).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    items = []
    for rec in raw:
        if not isinstance(rec, dict):
            continue
        item = _coerce_item(rec)
        if item:
            items.append(item)
    return _rank_items(items)


def _write_items(domain: str, items: list[PlaybookItem], outcome: str) -> None:
    PLAYBOOK_DIR.mkdir(parents=True, exist_ok=True)
    ranked = _rank_items(items)[:PLAYBOOK_MAX_ITEMS]
    _json_path(domain).write_text(
        json.dumps([asdict(i) for i in ranked], ensure_ascii=False, indent=2),
        encoding="utf-8")
    stamp = utc_now_iso()
    _path(domain).write_text(
        _render_items(ranked)[:PLAYBOOK_MAX_CHARS]
        + f"\n\n<!-- updated {stamp} after outcome={outcome} -->\n",
        encoding="utf-8")


def _parse_delta_items(text: str) -> list[PlaybookItem]:
    try:
        raw = json.loads(_strip_code_fence(text))
    except json.JSONDecodeError:
        return []
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    out = []
    for rec in items:
        if isinstance(rec, dict):
            item = _coerce_item(rec)
            if item:
                out.append(item)
    return out


def _markdown_delta_items(text: str) -> list[PlaybookItem]:
    out = []
    for line in text.splitlines():
        content = re.sub(r"^\s*[-*]\s+", "", line).strip()
        if not content or content.startswith("<!--") or content == "(nothing known yet)":
            continue
        out.append(PlaybookItem(id=_slug(content), surface="other", content=content))
    return out


def _merge_items(existing: list[PlaybookItem], delta: list[PlaybookItem], *,
                 outcome: str) -> list[PlaybookItem]:
    now = utc_now_iso()
    by_id = {item.id: item for item in existing}
    for item in delta:
        if not item.content:
            continue
        item.id = _slug(item.id or item.content)
        item.surface = _surface(item.surface)
        duplicate = by_id.get(item.id) or _find_similar(by_id.values(), item.content)
        if duplicate:
            duplicate.last_seen = now
            duplicate.evidence_count += 1
            if outcome == "submitted":
                duplicate.helpful += 1
            # Prefer the longer concrete wording unless it grows too large.
            if len(item.content) > len(duplicate.content) and len(item.content) <= 400:
                duplicate.content = item.content
            continue
        item.first_seen = item.first_seen or now
        item.last_seen = item.last_seen or now
        item.evidence_count = max(1, item.evidence_count)
        item.helpful = item.helpful + (1 if outcome == "submitted" else 0)
        by_id[item.id] = item
    return _rank_items(list(by_id.values()))


def _coerce_item(rec: dict[str, Any]) -> PlaybookItem | None:
    content = " ".join(str(rec.get("content") or "").split())
    if not content:
        return None
    return PlaybookItem(
        id=_slug(str(rec.get("id") or content)),
        surface=_surface(str(rec.get("surface") or "other")),
        content=content[:600],
        helpful=_int(rec.get("helpful")),
        harmful=_int(rec.get("harmful")),
        first_seen=str(rec.get("first_seen") or ""),
        last_seen=str(rec.get("last_seen") or ""),
        evidence_count=_int(rec.get("evidence_count")),
    )


def _render_items(items: list[PlaybookItem]) -> str:
    lines = []
    for item in _rank_items(items):
        suffix = []
        if item.evidence_count:
            suffix.append(f"seen {item.evidence_count}x")
        if item.helpful:
            suffix.append(f"helpful {item.helpful}x")
        meta = f" ({', '.join(suffix)})" if suffix else ""
        lines.append(f"- [{item.id}] {item.content}{meta}")
    return "\n".join(lines)


def _rank_items(items: list[PlaybookItem]) -> list[PlaybookItem]:
    return sorted(items, key=lambda i: (
        -(i.helpful * 3 + i.evidence_count - i.harmful * 2),
        i.surface,
        i.id,
    ))


def _find_similar(items, content: str) -> PlaybookItem | None:
    want = _tokens(content)
    if not want:
        return None
    for item in items:
        have = _tokens(item.content)
        if not have:
            continue
        overlap = len(want & have) / max(1, len(want | have))
        if overlap >= 0.72:
            return item
    return None


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]{3,}", text.lower()) if t}


def _surface(value: str) -> str:
    allowed = {
        "login", "apply_action", "dialog", "form", "upload",
        "upsell", "already_applied", "eligibility", "other",
    }
    value = re.sub(r"[^a-z_]+", "_", (value or "other").lower()).strip("_")
    return value if value in allowed else "other"


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", ".", (value or "").lower()).strip(".")
    return slug[:80] or "lesson"


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
