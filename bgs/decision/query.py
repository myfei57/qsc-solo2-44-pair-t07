"""Filtering of the record stream for the audit surfaces."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from ..errors import ValidationError
from ..store.records import Record


@dataclass(frozen=True, slots=True)
class RecordQuery:
    """Every filter the audit endpoint understands."""

    kinds: tuple[str, ...] = ()
    origins: tuple[str, ...] = ()
    generations: tuple[int, ...] = ()
    seq_from: int | None = None
    seq_to: int | None = None
    tick_from: int | None = None
    tick_to: int | None = None
    include_tombstones: bool = False
    limit: int | None = None

    def __post_init__(self) -> None:
        if self.limit is not None and self.limit < 1:
            raise ValidationError("query limit must be positive", limit=self.limit)
        if self.seq_from is not None and self.seq_to is not None and self.seq_from > self.seq_to:
            raise ValidationError("query sequence range is inverted", seq_from=self.seq_from, seq_to=self.seq_to)
        if self.tick_from is not None and self.tick_to is not None and self.tick_from > self.tick_to:
            raise ValidationError("query tick range is inverted", tick_from=self.tick_from, tick_to=self.tick_to)

    def matches(self, record: Record) -> bool:
        """Decide whether one record passes every configured filter."""

        if record.is_tombstone and not self.include_tombstones:
            return False
        if self.kinds and record.kind not in self.kinds:
            return False
        if self.origins and record.origin not in self.origins:
            return False
        if self.generations and record.generation not in self.generations:
            return False
        if self.seq_from is not None and record.seq < self.seq_from:
            return False
        if self.seq_to is not None and record.seq > self.seq_to:
            return False
        if self.tick_from is not None and record.tick < self.tick_from:
            return False
        if self.tick_to is not None and record.tick > self.tick_to:
            return False
        return True

    def apply(self, records: Iterable[Record]) -> tuple[Record, ...]:
        """Return the matching records, honouring the limit."""

        matched = [record for record in records if self.matches(record)]
        if self.limit is not None:
            matched = matched[-self.limit :]
        return tuple(matched)

    def describe(self) -> dict[str, Any]:
        return {
            "kinds": list(self.kinds),
            "origins": list(self.origins),
            "generations": list(self.generations),
            "seq_from": self.seq_from,
            "seq_to": self.seq_to,
            "tick_from": self.tick_from,
            "tick_to": self.tick_to,
            "include_tombstones": self.include_tombstones,
            "limit": self.limit,
        }


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Matching records plus a small summary."""

    query: RecordQuery
    records: tuple[Record, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        return len(self.records)

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.kind] = counts.get(record.kind, 0) + 1
        return counts

    def by_origin(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.origin] = counts.get(record.origin, 0) + 1
        return counts

    def describe(self) -> dict[str, Any]:
        return {
            "query": self.query.describe(),
            "total": self.total,
            "by_kind": self.by_kind(),
            "by_origin": self.by_origin(),
            "records": [
                {
                    "seq": record.seq,
                    "kind": record.kind,
                    "origin": record.origin,
                    "generation": record.generation,
                    "tick": record.tick,
                    "tombstone_of": record.tombstone_of,
                    "payload": dict(record.payload),
                }
                for record in self.records
            ],
        }


def run_query(records: Iterable[Record], query: RecordQuery) -> QueryResult:
    """Apply ``query`` and wrap the outcome."""

    return QueryResult(query=query, records=query.apply(records))
