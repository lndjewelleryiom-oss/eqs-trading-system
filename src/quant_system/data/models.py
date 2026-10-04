from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class TemporalLeakageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MarketObservation:
    dataset_id: str
    symbol: str
    event_time: datetime
    published_at: datetime
    available_at: datetime
    received_at: datetime
    value: float
    revision: int = 0
    confidence: float = 1.0
    suspected_corruption: bool = False

    def __post_init__(self) -> None:
        times = (self.event_time, self.published_at, self.available_at, self.received_at)
        if any(t.tzinfo is None or t.utcoffset() is None for t in times):
            raise ValueError("all timestamps must be timezone-aware")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        if self.revision < 0:
            raise ValueError("revision must be >= 0")

    def assert_usable_at(self, decision_time: datetime) -> None:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError("decision_time must be timezone-aware")
        if self.available_at > decision_time:
            raise TemporalLeakageError(
                f"observation {self.dataset_id}/{self.symbol} was unavailable at decision time"
            )
        if self.suspected_corruption:
            raise TemporalLeakageError("suspected-corrupt observation cannot be consumed")

    def is_stale(self, decision_time: datetime, max_age: timedelta) -> bool:
        self.assert_usable_at(decision_time)
        return decision_time - self.received_at > max_age


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
