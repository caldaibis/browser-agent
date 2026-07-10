"""Durable, deterministic memory of per-site gates — a *data* lever for fixes.

Production pattern this solves: the self-improvement agent correctly
diagnosed "your-house.nl wants €25 before you can apply" (twice in one day,
07-07-2026) but its only lever was editing code — so turning that diagnosis
into prevention needed a human commit. Same for ikwilhuren.nu's 5-concurrent-
viewing cap (diagnosed three times). A gate discovered at diagnosis time now
lands in `state/known_gates.json` via the agent's `record_known_gate` tool
and takes effect on the very next listing: no commit, no CI, no deploy,
reversible by deleting a JSON entry.

Gate kinds and how the pipeline consumes them:
- `paid_registration` — applying requires paying. Pre-flight in `apply.apply`
  short-circuits to outcome `payment_required` before the browser opens
  (merged into `_payment_required_reason`).
- `account_cap` — a temporary account-side limit (e.g. max concurrent viewing
  requests). Usually recorded with `expires_ts`; surfaces as a prompt warning
  so the agent stops early and cleanly instead of rediscovering it.
- `region_registration` — site needs a per-region inschrijving first
  (e.g. MijnDak). Prompt warning.
- `delayed_access` — listing access is delayed for non-paying accounts
  (e.g. ikwilhuren Plus). Prompt warning.
- `eligibility` — a site-wide hard eligibility mismatch. Prompt warning.

Only `paid_registration` blocks deterministically; everything else informs
the agent. Entries can expire (`expires_ts`); expired entries are ignored on
read, so a lapsed viewing cap heals itself. All reads fail open.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

from .config import PROJECT_ROOT
from .eventlog import parse_ts, utc_now_iso

GATES_PATH = PROJECT_ROOT / "state" / "known_gates.json"

GATE_KINDS = {
    "paid_registration",
    "account_cap",
    "region_registration",
    "delayed_access",
    "eligibility",
}


def normalize_domain(value: str) -> str:
    text = (value or "").strip().lower()
    if "://" in text:
        text = urlparse(text).hostname or ""
    text = text.split("/", 1)[0].split(":", 1)[0]
    return text[4:] if text.startswith("www.") else text


def load_gates(*, now: datetime | None = None) -> list[dict[str, Any]]:
    """All currently active (non-expired) gates. Fail-open: [] on any error."""
    now = now or datetime.now()
    try:
        data = json.loads(GATES_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    active = []
    for entry in data:
        if not isinstance(entry, dict) or entry.get("kind") not in GATE_KINDS:
            continue
        expires = parse_ts(entry.get("expires_ts"))
        if expires is not None and expires <= now:
            continue
        active.append(entry)
    return active


def gates_for_url(url: str) -> list[dict[str, Any]]:
    domain = normalize_domain(url)
    if not domain:
        return []
    return [g for g in load_gates() if g.get("domain") == domain]


def paid_registration_reason(url: str) -> str | None:
    """Pre-flight veto: a recorded paid gate on this domain, or None."""
    for gate in gates_for_url(url):
        if gate.get("kind") == "paid_registration":
            note = str(gate.get("note") or "").strip()
            return (
                f"{gate.get('domain')} has a recorded paid-registration gate"
                + (f": {note[:160]}" if note else "")
            )
    return None


def prompt_warnings(url: str) -> list[str]:
    """Non-blocking gate notes for `apply.build_prompt` (paid gates never get
    here — they short-circuit pre-flight)."""
    labels = {
        "account_cap": "ACCOUNT LIMIT",
        "region_registration": "REGISTRATION REQUIRED",
        "delayed_access": "DELAYED ACCESS",
        "eligibility": "ELIGIBILITY GATE",
    }
    out = []
    for gate in gates_for_url(url):
        label = labels.get(str(gate.get("kind") or ""))
        if not label:
            continue
        note = str(gate.get("note") or "").strip()[:200]
        expires = str(gate.get("expires_ts") or "")
        out.append(
            f"{label} on {gate.get('domain')}: {note or gate.get('kind')}"
            + (f" (until {expires})" if expires else "")
        )
    return out


def record_gate(*, domain: str, kind: str, note: str, source: str = "",
                expires_ts: str = "") -> str:
    """Add or update one gate. Returns a human-readable confirmation.

    Raises ValueError on invalid input so a misbehaving caller (the
    self-improvement agent's tool) gets a correctable error, not silence.
    """
    domain = normalize_domain(domain)
    if not domain or "." not in domain:
        raise ValueError(f"not a valid domain: {domain!r}")
    if kind not in GATE_KINDS:
        raise ValueError(f"unknown gate kind {kind!r}; valid: {sorted(GATE_KINDS)}")
    if expires_ts:
        try:
            datetime.fromisoformat(expires_ts)
        except ValueError:
            raise ValueError(f"expires_ts must be ISO-8601, got {expires_ts!r}") from None

    try:
        data = json.loads(GATES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = []
    except (OSError, json.JSONDecodeError):
        data = []

    entry = {
        "domain": domain,
        "kind": kind,
        "note": (note or "").strip()[:400],
        "source": (source or "").strip()[:120],
        "added_ts": utc_now_iso(),
    }
    if expires_ts:
        entry["expires_ts"] = expires_ts
    replaced = False
    for i, existing in enumerate(data):
        if (isinstance(existing, dict) and existing.get("domain") == domain
                and existing.get("kind") == kind):
            data[i] = entry
            replaced = True
            break
    if not replaced:
        data.append(entry)

    GATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(GATES_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, GATES_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    verb = "updated" if replaced else "recorded"
    return f"{verb} {kind} gate for {domain}" + (f" (expires {expires_ts})" if expires_ts else "")


def _write_gates(data: list[dict]) -> None:
    GATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(GATES_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, GATES_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def remove_gate(domain: str, kind: str) -> str:
    """Delete the gate for (domain, kind). Used by the dashboard to un-block a
    site the self-improvement agent gated wrongly (a wrongly-gated site
    silently skips every listing until the gate is cleared). Raises ValueError
    if no such gate exists so the caller can surface a clear message."""
    domain = normalize_domain(domain)
    if not domain:
        raise ValueError("empty domain")
    try:
        data = json.loads(GATES_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = []
    except (OSError, json.JSONDecodeError):
        data = []
    kept = [g for g in data if not (isinstance(g, dict)
            and g.get("domain") == domain and g.get("kind") == kind)]
    if len(kept) == len(data):
        raise ValueError(f"no {kind} gate for {domain}")
    _write_gates(kept)
    return f"removed {kind} gate for {domain}"
