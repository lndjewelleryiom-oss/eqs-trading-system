from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence

from ..models import BookUpdate, LiquidationEvent, PerpetualStateEvent, TradeEvent

NormalizedEvent = TradeEvent | BookUpdate | PerpetualStateEvent | LiquidationEvent


class UnknownInstrumentError(KeyError):
    pass


class UnsupportedMessageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class StreamSubscription:
    venue: str
    endpoint: str
    route_class: str
    channel: str
    venue_symbol: str
    subscribe_payload: dict[str, Any] | None = None


class CryptoPerpetualConnector(Protocol):
    venue: str

    def subscriptions(self, venue_symbol: str) -> tuple[StreamSubscription, ...]: ...

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        available_at: datetime,
        received_at: datetime,
        raw_sha256: str | None = None,
    ) -> tuple[NormalizedEvent, ...]: ...


class InstrumentMappedConnector:
    venue: str

    def __init__(self, instrument_map: Mapping[str, str]):
        self._instrument_map = dict(instrument_map)

    def instrument_id(self, venue_symbol: str) -> str:
        try:
            return self._instrument_map[venue_symbol]
        except KeyError as exc:
            raise UnknownInstrumentError(
                f"{self.venue} symbol {venue_symbol!r} is absent from the point-in-time instrument map"
            ) from exc

    @staticmethod
    def assert_ingestion_clock(available_at: datetime, received_at: datetime) -> None:
        for value, name in ((available_at, "available_at"), (received_at, "received_at")):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if available_at > received_at:
            raise ValueError("available_at cannot be after received_at")


def ms_timestamp(value: int | str) -> datetime:
    return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)


def sequence_string(*parts: object | None) -> str | None:
    present = [str(part) for part in parts if part is not None and str(part) != ""]
    return ":".join(present) if present else None
