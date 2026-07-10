"""Typed domain model — the pipeline's core records, defined once.

The listing dict used to flow through apply/orchestrator/poller/dashboard as
a bare `dict` with stringly-typed keys, and every JSONL record type was a
schema-by-convention at each producer and consumer. The dedup sagas
(Kaatstraat, Hof van Oslo — see docs/lessons/) were at root "same entity,
several ad-hoc key representations" bugs. These dataclasses centralize the
two things that kept going wrong:

- **Field names**: a typo'd key is now an AttributeError at the source, not
  a silent None three modules later.
- **Identity**: `Listing.dedup_keys()` / `ProcessedRecord.keys()` are the
  ONE place that derives every key form (raw + canonical, source/stekkies/
  resolved) for a real-world listing.

Wire compatibility: `from_json` accepts any historical record shape
(unknown keys ignored, missing keys defaulted) and `to_json` writes the
same JSON shape the JSONL files and dashboard already read, so records are
interchangeable with the old dicts at every boundary.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any

from .poller.dedup import canonical_url


def _str_of(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    return "" if value is None else str(value)


@dataclass(frozen=True)
class Listing:
    """One rental listing on its way to (or through) the apply stage.

    ``source_url`` is the only required field — it is both where the agent
    applies and the primary dedup identity. Everything else enriches the
    prompt, filters, and logs.
    """
    source_url: str
    source_name: str = ""
    address: str = ""
    price: str = ""
    title: str = ""
    description: str = ""
    stekkies_url: str = ""
    detected_by: str = ""

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Listing:
        """Tolerant load from any historical listing dict; extra keys are
        ignored, missing ones default. Raises ValueError without source_url."""
        source_url = _str_of(data, "source_url").strip()
        if not source_url:
            raise ValueError("listing has no source_url")
        return cls(
            source_url=source_url,
            source_name=_str_of(data, "source_name") or _str_of(data, "source"),
            address=_str_of(data, "address"),
            price=_str_of(data, "price"),
            title=_str_of(data, "title"),
            description=_str_of(data, "description"),
            stekkies_url=_str_of(data, "stekkies_url") or _str_of(data, "listing_url"),
            detected_by=_str_of(data, "detected_by"),
        )

    def to_json(self) -> dict[str, Any]:
        """The wire dict every existing consumer (prompt logs, playbooks,
        self-improvement context, JSONL records) already understands."""
        return {k: v for k, v in asdict(self).items() if v}

    def dedup_keys(self) -> frozenset[str]:
        """Every identity this listing is known under: raw and canonical
        forms of the source and Stekkies URLs."""
        keys: set[str] = set()
        for url in (self.source_url, self.stekkies_url):
            if url:
                keys.add(url)
                keys.add(canonical_url(url))
        return frozenset(keys)


@dataclass(frozen=True)
class ProcessedRecord:
    """One line of processed_listings.jsonl / row of the store's
    processed_listings table: a listing some path finished handling."""
    ts: str = ""
    trigger: str = ""
    msg_id: str = ""
    source_url: str = ""
    stekkies_url: str = ""
    resolved_url: str = ""   # real destination reached mid-flight (aggregators)
    source: str = ""
    detected_by: str = ""
    address: str = ""
    outcome: str = ""

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ProcessedRecord:
        known = {f.name for f in fields(cls)}
        return cls(**{k: _str_of(data, k) for k in known})

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v}

    def keys(self) -> frozenset[str]:
        """Every dedup key this record contributes: raw + canonical forms of
        stekkies/source/resolved URLs. THE one derivation both the
        orchestrator pre-flight and the poller's SeenStore must share —
        having two copies is exactly how the cross-source duplicate
        submission of 02-07-2026 happened."""
        keys: set[str] = set()
        for url in (self.stekkies_url, self.source_url, self.resolved_url):
            if url:
                keys.add(url)
                keys.add(canonical_url(url))
        return frozenset(keys)
