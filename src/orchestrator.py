"""End-to-end runner:  Gmail trigger -> Stekkies extract -> Hermes apply.

Run:  python -m src.orchestrator           # live watch loop
      python -m src.orchestrator --once URL # process one Stekkies URL and exit
"""
import json
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime

from .config import LOG_DIR, PROJECT_ROOT
from .stekkies import extract_listing
from .apply import apply
from .gmail_watch import mark_read, watch


PROCESSED_FILE = PROJECT_ROOT / "state" / "processed_listings.jsonl"
ACTIVITY_LOG = LOG_DIR / "activity.log"
MAIL_SUMMARY_LOG = LOG_DIR / "mail_summary.jsonl"


def _log(event: str, **kw) -> None:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **kw}
    with (LOG_DIR / "runs.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{rec['ts']}] {event}: " + " ".join(f"{k}={v}" for k, v in kw.items()))


def _activity(message: str) -> None:
    ts = datetime.now().isoformat(timespec="seconds")
    line = f"[{ts}] {message}"
    with ACTIVITY_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _mail_summary(**kw) -> dict:
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), **kw}
    with MAIL_SUMMARY_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def _processed_keys() -> set[str]:
    keys: set[str] = set()
    if not PROCESSED_FILE.exists():
        return keys
    for line in PROCESSED_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        for field in ("stekkies_url", "source_url"):
            value = rec.get(field)
            if value:
                keys.add(value)
    return keys


def _remember_processed(**kw) -> None:
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), **kw}
    with PROCESSED_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _finish(**kw) -> dict:
    rec = _mail_summary(**kw)
    msg_id = rec.get("msg_id") or "-"
    status = rec.get("status")
    detail = rec.get("message")
    address = rec.get("address") or "unknown address"
    source = rec.get("source") or "unknown source"
    _activity(f"mail={msg_id} status={status} source={source} address={address} - {detail}")
    return rec


def process(stekkies_url: str, msg_id: str | None = None) -> dict:
    t0 = time.time()
    _log("listing_received", msg_id=msg_id, url=stekkies_url)
    if stekkies_url in _processed_keys():
        _log("duplicate_listing_skipped", msg_id=msg_id, url=stekkies_url)
        return _finish(
            msg_id=msg_id,
            stekkies_url=stekkies_url,
            status="skipped_duplicate",
            mark_read=True,
            message="Skipped because this Stekkies listing was already handled.",
        )

    try:
        listing = extract_listing(stekkies_url, headless=True)
        d = asdict(listing)
        (LOG_DIR / "last_listing.json").write_text(
            json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
        _log("listing_extracted", source=listing.source_name,
             source_url=listing.source_url, address=listing.address)
        if not listing.source_url:
            _log("no_source_url", note="cannot apply without external link")
            return _finish(
                msg_id=msg_id,
                stekkies_url=stekkies_url,
                source=listing.source_name,
                address=listing.address,
                status="no_source_url",
                mark_read=True,
                message="Could not find an external source URL, so no application was submitted.",
            )
        if listing.source_url in _processed_keys():
            _log("duplicate_source_skipped", msg_id=msg_id, source_url=listing.source_url)
            return _finish(
                msg_id=msg_id,
                stekkies_url=stekkies_url,
                source_url=listing.source_url,
                source=listing.source_name,
                address=listing.address,
                status="skipped_duplicate",
                mark_read=True,
                message="Skipped because this external listing was already handled.",
            )
        result = apply(d)
        _log("applied", outcome=result.outcome, returncode=result.rc,
             seconds=round(time.time() - t0, 1))
        # Record (so we don't retry) only when retrying wouldn't help: a real
        # submission or a terminal site state (already applied / unavailable /
        # not eligible / login needed). Transient failures stay retryable.
        if result.terminal:
            _remember_processed(
                msg_id=msg_id,
                stekkies_url=stekkies_url,
                source_url=listing.source_url,
                source=listing.source_name,
                address=listing.address,
                outcome=result.outcome,
            )
        return _finish(
            msg_id=msg_id,
            stekkies_url=stekkies_url,
            source_url=listing.source_url,
            source=listing.source_name,
            address=listing.address,
            status=result.outcome,
            mark_read=True,
            returncode=result.rc,
            seconds=round(time.time() - t0, 1),
            message=result.summary or f"Agent finished with outcome={result.outcome} (rc={result.rc}).",
        )
    except Exception as e:
        _log("error", error=str(e))
        traceback.print_exc()
        return _finish(
            msg_id=msg_id,
            stekkies_url=stekkies_url,
            status="error",
            mark_read=True,
            seconds=round(time.time() - t0, 1),
            message=f"{type(e).__name__}: {e}. Check runs.jsonl and the service journal for the traceback.",
        )


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--once":
        process(sys.argv[2])
        return 0
    _log("watcher_started")
    for msg_id, url in watch():
        if not url:
            result = _finish(
                msg_id=msg_id,
                status="no_listing_link",
                mark_read=True,
                message="Stekkies email matched the Gmail query but no listing link was found.",
            )
        else:
            result = process(url, msg_id=msg_id)
        if result.get("mark_read"):
            try:
                mark_read(msg_id)
                _log("mail_marked_read", msg_id=msg_id, status=result.get("status"))
            except Exception as e:
                _log("mail_mark_read_failed", msg_id=msg_id, error=str(e))
                _activity(
                    f"mail={msg_id} status=mark_read_failed source=unknown source "
                    f"address=unknown address - {type(e).__name__}: {e}"
                )
    return 0


if __name__ == "__main__":
    sys.exit(main())
