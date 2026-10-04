from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .models import BookUpdate, EventKind, MarketDataMeta


class SequenceIntegrityError(ValueError):
    pass


@dataclass(slots=True)
class BookSequenceGuard:
    """Fail-closed continuity guard for normalized order-book updates.

    Exact gap checks are applied when the venue supplies previous-sequence or first/final
    sequence bounds. Otherwise the guard still rejects non-increasing update IDs.
    A snapshot resets the state, matching exchange restart/resnapshot semantics.
    """

    last_sequence: int | None = None

    def accept(self, update: BookUpdate) -> None:
        final = update.final_sequence
        if update.meta.kind == EventKind.BOOK_SNAPSHOT:
            if final is None:
                raise SequenceIntegrityError("snapshot lacks a final sequence")
            self.last_sequence = final
            return
        if final is None:
            raise SequenceIntegrityError("delta lacks a final sequence")
        if self.last_sequence is None:
            raise SequenceIntegrityError("delta received before order-book initialization")
        if update.previous_sequence is not None and update.previous_sequence != self.last_sequence:
            raise SequenceIntegrityError("previous-sequence mismatch")
        if update.first_sequence is not None and update.previous_sequence is None:
            if update.first_sequence > self.last_sequence + 1:
                raise SequenceIntegrityError("sequence gap")
        if final <= self.last_sequence:
            raise SequenceIntegrityError("non-increasing order-book sequence")
        self.last_sequence = final


@dataclass(frozen=True, slots=True)
class LatencyAssessment:
    publication_to_availability: timedelta
    availability_to_ingestion: timedelta


def assess_latency(meta: MarketDataMeta) -> LatencyAssessment:
    return LatencyAssessment(
        publication_to_availability=meta.available_at - meta.published_at,
        availability_to_ingestion=meta.received_at - meta.available_at,
    )
