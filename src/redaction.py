"""Secret redaction — one implementation, applied wherever text leaves the
process (event logs, dashboard views, alert emails, trajectories).

Secrets are the stored site credentials (state/sources_credentials.json) plus
any password-shaped line. Fail-open: if the credential file is unreadable the
pattern-based rules still apply.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from .config import PROJECT_ROOT

CREDS_FILE = PROJECT_ROOT / "state" / "sources_credentials.json"


@lru_cache(maxsize=1)
def _secret_values() -> tuple[str, ...]:
    vals: set[str] = set()
    try:
        creds = json.loads(CREDS_FILE.read_text(encoding="utf-8"))
        for c in creds.values():
            for k in ("password", "username"):
                v = (c.get(k) or "").strip()
                if len(v) >= 4:
                    vals.add(v)
    except Exception:
        pass
    # longest first so substrings don't pre-empt longer secrets
    return tuple(sorted(vals, key=len, reverse=True))


def redact(text: str) -> str:
    if not text:
        return text
    for v in _secret_values():
        text = text.replace(v, "***")
    text = re.sub(r"(?im)^(\s*password:).*$", r"\1 ***", text)
    text = re.sub(r"(?im)^(\s*username(?:/email)?:).*$", r"\1 ***", text)
    # belt-and-braces: redact any 'password' value the agent may have logged
    text = re.sub(r"(?i)(password['\"]?\s*[:=]\s*)\S+", r"\1***", text)
    return text
