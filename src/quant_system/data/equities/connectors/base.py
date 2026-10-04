from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Protocol

from ..models import CorporateActionEvent, EquityBarEvent, EquityQuoteEvent, EquityTradeEvent

NormalizedEquityEvent = EquityTradeEvent | EquityQuoteEvent | EquityBarEvent | CorporateActionEvent


class UnknownEquityInstrumentError(KeyError):
    pass


class UnsupportedEquityMessageError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EquityStreamSubscription:
    venue: str
    endpoint: str
    route_class: str
    channel: str
    venue_symbol: str
    subscribe_payload: dict[str, Any] | None = None


class EquityMarketConnector(Protocol):
    venue: str

    def subscriptions(self, venue_symbol: str) -> tuple[EquityStreamSubscription, ...]: ...

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        available_at: datetime,
        received_at: datetime,
        raw_sha256: str | None = None,
    ) -> tuple[NormalizedEquityEvent, ...]: ...


class EquityInstrumentMappedConnector:
    venue: str

    def __init__(self, instrument_map: Mapping[str, str]):
        self._instrument_map = dict(instrument_map)

    def instrument_id(self, venue_symbol: str) -> str:
        try:
            return self._instrument_map[venue_symbol]
        except KeyError as exc:
            raise UnknownEquityInstrumentError(
                f"{self.venue} symbol {venue_symbol!r} is absent from the point-in-time instrument map"
            ) from exc

    @staticmethod
    def assert_ingestion_clock(available_at: datetime, received_at: datetime) -> None:
        for value, name in ((available_at, "available_at"), (received_at, "received_at")):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{name} must be timezone-aware")
        if available_at > received_at:
            raise ValueError("available_at cannot be after received_at")
