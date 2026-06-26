"""End-to-end runner:  Gmail trigger -> Stekkies extract -> Hermes apply.

Run:  python -m src.orchestrator           # live watch loop
      python -m src.orchestrator --once URL # process one Stekkies URL and exit
"""
import json
import sys
import time
import traceback
from datetime import datetime

from .config import LOG_DIR
from .stekkies import extract_listing
from .apply_hermes import apply
from .gmail_watch import watch
from dataclasses import asdict


def _log(event: str, **kw) -> None:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
    with (LOG_DIR / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{rec['ts']}] {event}: " + " ".join(f"{k}={v}" for k, v in kw.items()))


def process(stekkies_url: str) -> None:
    t0 = time.time()
    _log("listing_received", url=stekkies_url)
    try:
        listing = extract_listing(stekkies_url, headless=True)
        d = asdict(listing)
        (LOG_DIR / "last_listing.json").write_text(
            json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        _log("listing_extracted", source=listing.source_name,
             source_url=listing.source_url, address=listing.address)
        if not listing.source_url:
            _log("no_source_url", note="cannot apply without external link")
            return
        rc = apply(d)
        _log("applied", returncode=rc, seconds=round(time.time() - t0, 1))
    except Exception as e:
        _log("error", error=str(e))
        traceback.print_exc()


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        process(sys.argv[2])
        return 0
    _log("watcher_started")
    for _msg_id, url in watch():
        process(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
