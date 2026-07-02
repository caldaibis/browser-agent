"""End-to-end runner: Gmail listing trigger -> extract/direct listing -> apply.

Run:  python -m src.orchestrator            # live watch loop
      python -m src.orchestrator --once URL # process one Stekkies URL and exit
"""
import json
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime

from .apply_priority import priority_claim
from .config import LOG_DIR, PROJECT_ROOT
from .stekkies import extract_listing
from .apply import apply
from .gmail_watch import message_received_ts, mark_read, watch_events
from .poller.dedup import active_claim_keys, canonical_url
from .notify import send_status_email
from .self_improvement_agent import improve_after_apply, improve_exception


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
                keys.add(canonical_url(value))
    return keys


def _remember_processed(**kw) -> None:
    PROCESSED_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now().isoformat(timespec="seconds"), **kw}
    with PROCESSED_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# Outcomes not worth emailing about (pure bookkeeping, no real attempt).
_NO_EMAIL_STATUSES = {"skipped_duplicate", "no_listing_link"}


def _finish(**kw) -> dict:
    kw.setdefault("trigger", "stekkies_mail" if kw.get("msg_id") else "manual")
    rec = _mail_summary(**kw)
    msg_id = rec.get("msg_id") or "-"
    status = rec.get("status")
    detail = rec.get("message")
    address = rec.get("address") or "unknown address"
    source = rec.get("source") or "unknown source"
    _activity(f"mail={msg_id} status={status} source={source} address={address} - {detail}")
    if status not in _NO_EMAIL_STATUSES:
        send_status_email(rec)
    return rec


def _source_duplicate(source_url: str, keys: set[str]) -> bool:
    key = canonical_url(source_url)
    return source_url in keys or key in keys or key in active_claim_keys()


def process_source(listing: dict, msg_id: str | None = None,
                   trigger: str = "manual", msg_received_ts: str = "") -> dict:
    """Apply directly to an external source listing, used by Huurwoningen mail
    and manual/direct integrations that do not need Stekkies extraction."""
    t0 = time.time()
    source_url = listing["source_url"]
    source = listing.get("source_name") or listing.get("source") or "unknown source"
    address = listing.get("address") or "unknown address"
    _log("source_listing_received", msg_id=msg_id, trigger=trigger,
         source=source, source_url=source_url, address=address)

    if _source_duplicate(source_url, _processed_keys()):
        _log("duplicate_source_skipped", msg_id=msg_id, source_url=source_url)
        return _finish(
            msg_id=msg_id,
            trigger=trigger,
            msg_received_ts=msg_received_ts,
            source_url=source_url,
            source=source,
            address=address,
            status="skipped_duplicate",
            mark_read=True,
            message="Skipped because this external listing was already handled.",
        )

    try:
        (LOG_DIR / "last_listing.json").write_text(
            json.dumps(listing, indent=2, ensure_ascii=False), encoding="utf-8")
        # Priority claim: the poller defers/yields the shared browser to this
        # mail/manual apply for the whole run (see apply_priority.py).
        with priority_claim():
            result = apply(listing)
        _log("applied", outcome=result.outcome, returncode=result.rc,
             seconds=round(time.time() - t0, 1))
        improve_after_apply(
            listing=listing,
            result=result,
            trigger=trigger,
            msg_id=msg_id,
            extra={"source_url": source_url, "source": source, "address": address},
        )
        # One attempt per listing — record every completed agent run, terminal
        # or not, so neither this path nor the poller ever re-applies to it (a
        # retry re-runs the identical prompt at full cost for the same result).
        _remember_processed(
            msg_id=msg_id,
            trigger=trigger,
            source_url=source_url,
            source=source,
            address=address,
            outcome=result.outcome,
            **({"resolved_url": result.resolved_url} if result.resolved_url else {}),
        )
        return _finish(
            msg_id=msg_id,
            trigger=trigger,
            msg_received_ts=msg_received_ts,
            source_url=source_url,
            source=source,
            address=address,
            status=result.outcome,
            mark_read=True,
            returncode=result.rc,
            seconds=round(time.time() - t0, 1),
            message=result.summary or f"Agent finished with outcome={result.outcome} (rc={result.rc}).",
        )
    except Exception as e:
        _log("error", error=str(e))
        traceback.print_exc()
        improve_exception(
            listing=listing,
            error=e,
            trigger=trigger,
            msg_id=msg_id,
            extra={"source_url": source_url, "source": source, "address": address},
        )
        return _finish(
            msg_id=msg_id,
            trigger=trigger,
            msg_received_ts=msg_received_ts,
            source_url=source_url,
            source=source,
            address=address,
            status="error",
            mark_read=True,
            seconds=round(time.time() - t0, 1),
            message=f"{type(e).__name__}: {e}. Check runs.jsonl and the service journal for the traceback.",
        )


def process(stekkies_url: str, msg_id: str | None = None,
            trigger: str | None = None) -> dict:
    t0 = time.time()
    received_ts = message_received_ts(msg_id) if msg_id else None
    _log("listing_received", msg_id=msg_id, url=stekkies_url)
    keys = _processed_keys()
    if _source_duplicate(stekkies_url, keys):
        _log("duplicate_listing_skipped", msg_id=msg_id, url=stekkies_url)
        return _finish(
            msg_id=msg_id,
            trigger=trigger or ("stekkies_mail" if msg_id else "manual"),
            msg_received_ts=received_ts,
            stekkies_url=stekkies_url,
            status="skipped_duplicate",
            mark_read=True,
            message="Skipped because this Stekkies listing was already handled.",
        )

    try:
        # Priority claim: the poller defers/yields the shared browser to this
        # mail/manual run (see apply_priority.py). It spans extraction too —
        # extraction drives the browser as well — but NOT improve_after_apply
        # below (a self-improvement run can take minutes and must not starve
        # the poller).
        with priority_claim():
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
                    trigger=trigger or ("stekkies_mail" if msg_id else "manual"),
                    msg_received_ts=received_ts,
                    stekkies_url=stekkies_url,
                    source=listing.source_name,
                    address=listing.address,
                    status="no_source_url",
                    mark_read=True,
                    message="Could not find an external source URL, so no application was submitted.",
                )
            if _source_duplicate(listing.source_url, keys):
                _log("duplicate_source_skipped", msg_id=msg_id, source_url=listing.source_url)
                return _finish(
                    msg_id=msg_id,
                    trigger=trigger or ("stekkies_mail" if msg_id else "manual"),
                    msg_received_ts=received_ts,
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
        improve_after_apply(
            listing=d,
            result=result,
            trigger=trigger or ("stekkies_mail" if msg_id else "manual"),
            msg_id=msg_id,
            extra={"stekkies_url": stekkies_url},
        )
        # One attempt per listing — record every completed agent run, terminal
        # or not, so neither this path nor the poller ever re-applies to it (a
        # retry re-runs the identical prompt at full cost for the same result).
        _remember_processed(
            msg_id=msg_id,
            stekkies_url=stekkies_url,
            source_url=listing.source_url,
            source=listing.source_name,
            address=listing.address,
            outcome=result.outcome,
            **({"resolved_url": result.resolved_url} if result.resolved_url else {}),
        )
        return _finish(
            msg_id=msg_id,
            trigger=trigger or ("stekkies_mail" if msg_id else "manual"),
            msg_received_ts=received_ts,
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
        improve_exception(
            listing={"stekkies_url": stekkies_url},
            error=e,
            trigger=trigger or ("stekkies_mail" if msg_id else "manual"),
            msg_id=msg_id,
        )
        return _finish(
            msg_id=msg_id,
            trigger=trigger or ("stekkies_mail" if msg_id else "manual"),
            msg_received_ts=received_ts,
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
    for ev in watch_events():
        msg_id = ev.msg_id
        if not ev.url:
            result = _finish(
                msg_id=msg_id,
                trigger=ev.trigger,
                msg_received_ts=ev.received_ts or message_received_ts(msg_id),
                status="no_listing_link",
                mark_read=True,
                message=f"{ev.provider} email matched the Gmail query but no listing link was found.",
            )
        elif ev.provider == "stekkies":
            result = process(ev.url, msg_id=msg_id, trigger=ev.trigger)
        elif ev.provider == "huurwoningen":
            listing = {
                "source_url": ev.url,
                "source_name": "huurwoningen.nl",
                "address": ev.address or ev.subject or "?",
                "price": ev.price or "?",
            }
            result = process_source(
                listing,
                msg_id=msg_id,
                trigger=ev.trigger,
                msg_received_ts=ev.received_ts or message_received_ts(msg_id),
            )
        else:
            result = _finish(
                msg_id=msg_id,
                trigger=ev.trigger,
                msg_received_ts=ev.received_ts or message_received_ts(msg_id),
                status="no_listing_link",
                mark_read=True,
                message=f"Unsupported Gmail provider: {ev.provider}",
            )
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
