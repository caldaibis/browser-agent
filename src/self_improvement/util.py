"""Context redaction for everything the self-improvement agent logs/prompts."""
from __future__ import annotations

from typing import Any

from ..redaction import redact


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
