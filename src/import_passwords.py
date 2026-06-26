"""Import a Google Password Manager CSV export into sources_credentials.json.

Keys each entry by registrable domain (last two labels of the host), so it
matches the Stekkies source URL host in credentials.for_url().

Run:  python -m src.import_passwords passwords.csv
Then delete the CSV.
"""
import csv
import json
import sys
from urllib.parse import urlparse

from .config import PROJECT_ROOT
from .credentials import CRED_FILE


def registrable_domain(url_or_host: str) -> str:
    host = urlparse(url_or_host).hostname or url_or_host
    host = host.lower().strip()
    labels = host.split(".")
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def main() -> int:
    src = sys.argv[1] if len(sys.argv) > 1 else str(PROJECT_ROOT / "passwords.csv")
    out: dict[str, dict] = {}
    with open(src, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            url = (row.get("url") or row.get("name") or "").strip()
            user = (row.get("username") or "").strip()
            pw = (row.get("password") or "").strip()
            if not (url and user and pw):
                continue
            # Skip password-reset / token links — not real login creds.
            low = url.lower()
            if any(s in low for s in ("herinstellen", "wachtwoord/herinstellen",
                                       "password_reset", "reset")):
                continue
            domain = registrable_domain(url)
            out.setdefault(domain, {"username": user, "password": pw})  # keep first
    CRED_FILE.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {len(out)} entries to {CRED_FILE}:")
    for d in sorted(out):
        print("  -", d, "->", out[d]["username"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
