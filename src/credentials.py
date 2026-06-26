"""Per-source-site login credentials for the apply agent.

Stored locally in state/sources_credentials.json (gitignored). Format:

{
  "ikwilhuren.nu": {"username": "you@example.com", "password": "..."},
  "pararius.nl":   {"username": "...",            "password": "..."},
  "kamernet.nl":   {"username": "...",            "password": "..."}
}

Matching is by domain substring against the source URL's host, so
"pararius.nl" matches www.pararius.nl. Returns None if no entry.
"""
import json
from urllib.parse import urlparse

from .config import PROJECT_ROOT

CRED_FILE = PROJECT_ROOT / "state" / "sources_credentials.json"


def _load() -> dict:
    if not CRED_FILE.exists():
        return {}
    return json.loads(CRED_FILE.read_text(encoding="utf-8"))


def for_url(url: str) -> dict | None:
    host = (urlparse(url).hostname or "").lower()
    for key, cred in _load().items():
        if key.lower() in host:
            return cred
    return None
