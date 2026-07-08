"""Per-run trajectory timeline for the forensics detail page.

Primary source: `logs/trajectories/{stem}.jsonl` (structured events written by
browser_agent — run_start, turn_usage, tool_call, tool_result, guard, final).
Fallback for runs that predate trajectories: parse the `[agent] turn N …`
lines out of the (already redacted) transcript text.

Both produce the same shape: a list of `TurnView`s plus run meta/final, so the
template renders one way. Everything here is defensive: unknown/missing data
yields an empty timeline, never an exception.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..config import LOG_DIR
from . import cache
from .data import redact

TRAJECTORY_DIR = LOG_DIR / "trajectories"


@dataclass
class ToolEvent:
    tool: str
    ok: bool | None = None
    detail: str = ""


@dataclass
class TurnView:
    turn: int
    calls: list[ToolEvent] = field(default_factory=list)
    results: list[ToolEvent] = field(default_factory=list)
    guards: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    finish_reason: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cache_hit_tokens: int | None = None


@dataclass
class Timeline:
    source: str            # "trajectory" | "transcript"
    model: str = ""
    turns: list[TurnView] = field(default_factory=list)
    final_outcome: str = ""
    final_reason: str = ""

    @property
    def has_tokens(self) -> bool:
        return any(t.completion_tokens is not None for t in self.turns)


def load_timeline(stem: str) -> Timeline | None:
    if not stem:
        return None
    path = TRAJECTORY_DIR / f"{stem}.jsonl"
    records = cache.jsonl_records(path)
    if not records:
        return None
    turns: dict[int, TurnView] = {}

    def _turn(n: int) -> TurnView:
        return turns.setdefault(n, TurnView(turn=n))

    tl = Timeline(source="trajectory")
    for rec in records:
        event = rec.get("event")
        payload = rec.get("payload") or {}
        turn = int(payload.get("turn") or 0)
        if event == "run_start":
            tl.model = str(payload.get("model") or "")
        elif event == "turn_usage":
            tv = _turn(turn)
            tv.finish_reason = str(payload.get("finish_reason") or "")
            tv.prompt_tokens = _int(payload.get("prompt_tokens"))
            tv.completion_tokens = _int(payload.get("completion_tokens"))
            tv.cache_hit_tokens = _int(payload.get("cache_hit_tokens"))
        elif event == "tool_call":
            _turn(turn).calls.append(ToolEvent(
                tool=str(payload.get("tool") or "?"),
                detail=redact(_clip(payload.get("args"))),
            ))
        elif event == "tool_result":
            _turn(turn).results.append(ToolEvent(
                tool=str(payload.get("tool") or "?"),
                ok=payload.get("ok"),
                detail=redact(_clip(payload.get("summary")
                                    or payload.get("error")
                                    or (f"{payload.get('chars')} chars"
                                        if payload.get("chars") is not None else ""))),
            ))
        elif event == "guard":
            _turn(turn).guards.append(str(payload.get("name") or "guard"))
        elif event == "assistant_text":
            txt = redact(str(payload.get("text") or "")).strip()
            if txt:
                _turn(turn).texts.append(txt)
        elif event == "final":
            tl.final_outcome = str(payload.get("outcome") or "")
            tl.final_reason = str(payload.get("reason") or "")
    tl.turns = [turns[k] for k in sorted(turns)]
    return tl


_TURN_NO_RE = re.compile(r"\[agent\]\s+turn\s+(\d+)\b")
_TURN_CALL_RE = re.compile(r"\[agent\]\s+turn\s+(\d+)\s+call\s+(\S+)\s*(.*)")
_FINISH_RE = re.compile(r"finish=(\S+)")
_FIELD_RE = re.compile(r"([a-z_]+)=([0-9]+|None)")
_MODEL_RE = re.compile(r"\[agent\]\s+model=(\S+)")


def timeline_from_transcript(text: str) -> Timeline | None:
    """Best-effort timeline from an older transcript's `[agent]` log lines."""
    if not text:
        return None
    turns: dict[int, TurnView] = {}
    tl = Timeline(source="transcript")

    def _turn(n: int) -> TurnView:
        return turns.setdefault(n, TurnView(turn=n))

    for line in text.splitlines():
        m = _MODEL_RE.search(line)
        if m and not tl.model:
            tl.model = m.group(1)
        m = _TURN_CALL_RE.search(line)
        if m:
            _turn(int(m.group(1))).calls.append(
                ToolEvent(tool=m.group(2), detail=_clip(m.group(3))))
            continue
        if "completion_tokens=" in line:
            tm = _TURN_NO_RE.search(line)
            if not tm:
                continue
            tv = _turn(int(tm.group(1)))
            fm = _FINISH_RE.search(line)
            if fm:
                tv.finish_reason = fm.group(1)
            fields = {k: _int(v) for k, v in _FIELD_RE.findall(line)}
            tv.prompt_tokens = fields.get("prompt_tokens", tv.prompt_tokens)
            tv.completion_tokens = fields.get("completion_tokens", tv.completion_tokens)
            tv.cache_hit_tokens = fields.get("cache_hit_tokens", tv.cache_hit_tokens)
    if not turns:
        return None
    tl.turns = [turns[k] for k in sorted(turns)]
    return tl


def _int(value: Any) -> int | None:
    try:
        if value is None or value == "None":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _clip(value: Any, limit: int = 200) -> str:
    s = value if isinstance(value, str) else ("" if value is None else str(value))
    s = s.strip()
    return s if len(s) <= limit else s[:limit] + "…"
