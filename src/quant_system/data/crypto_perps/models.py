from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any


class EventKind(StrEnum):
    TRADE = "TRADE"
    BOOK_SNAPSHOT = "BOOK_SNAPSHOT"
    BOOK_DELTA = "BOOK_DELTA"
    PERPETUAL_STATE = "PERPETUAL_STATE"
    LIQUIDATION = "LIQUIDATION"


class LiquidatedSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


def _assert_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class MarketDataMeta:
    """Point-in-time provenance shared by every normalized market-data event.

    event_time: economic/matching-engine event time when supplied by the venue.
    published_at: venue/system message-generation timestamp when supplied.
    available_at: local socket-receive timestamp; first instant this collector could know it.
    received_at: normalization/ingestion timestamp after receipt.

    The ordering is intentionally strict. Clock-skewed or impossible observations must be
    quarantined before research rather than silently time-warped into the past.
    """

    venue: str
    instrument_id: str
    venue_symbol: str
    kind: EventKind
    event_time: datetime
    published_at: datetime
    available_at: datetime
    received_at: datetime
    source_channel: str
    source_sequence: str | None
    raw_sha256: str
    schema_version: str = "crypto-perps-v1"

    def __post_init__(self) -> None:
        for name in ("event_time", "published_at", "available_at", "received_at"):
            _assert_aware(getattr(self, name), name)
        if not self.venue or not self.instrument_id or not self.venue_symbol:
            raise ValueError("venue, instrument_id and venue_symbol are required")
        if self.event_time > self.published_at:
            raise ValueError("event_time cannot be after published_at")
        if self.published_at > self.available_at:
            raise ValueError("published_at cannot be after available_at")
        if self.available_at > self.received_at:
            raise ValueError("available_at cannot be after received_at")
        if len(self.raw_sha256) != 64:
            raise ValueError("raw_sha256 must be a SHA-256 hex digest")

    def assert_usable_at(self, decision_time: datetime) -> None:
        _assert_aware(decision_time, "decision_time")
        if self.available_at > decision_time:
            raise ValueError("market-data event was not available at decision time")

    def canonical_identity(self) -> str:
        payload = {
            "venue": self.venue,
            "instrument_id": self.instrument_id,
            "venue_symbol": self.venue_symbol,
            "kind": self.kind,
            "event_time": self.event_time.isoformat(),
            "published_at": self.published_at.isoformat(),
            "source_channel": self.source_channel,
            "source_sequence": self.source_sequence,
            "raw_sha256": self.raw_sha256,
            "schema_version": self.schema_version,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError("book price must be positive")
        if self.quantity < 0:
            raise ValueError("book quantity cannot be negative")


@dataclass(frozen=True, slots=True)
class TradeEvent:
    meta: MarketDataMeta
    trade_id: str
    price: Decimal
    quantity: Decimal
    aggressor_side: str | None

    def __post_init__(self) -> None:
        if self.meta.kind != EventKind.TRADE:
            raise ValueError("TradeEvent requires TRADE metadata")
        if self.price <= 0 or self.quantity <= 0:
            raise ValueError("trade price and quantity must be positive")
        if self.aggressor_side not in (None, "BUY", "SELL"):
            raise ValueError("aggressor_side must be BUY, SELL or None")


@dataclass(frozen=True, slots=True)
class BookUpdate:
    meta: MarketDataMeta
    bids: tuple[BookLevel, ...]
    asks: tuple[BookLevel, ...]
    first_sequence: int | None = None
    final_sequence: int | None = None
    previous_sequence: int | None = None
    checksum: int | None = None

    def __post_init__(self) -> None:
        if self.meta.kind not in (EventKind.BOOK_SNAPSHOT, EventKind.BOOK_DELTA):
            raise ValueError("BookUpdate requires BOOK_SNAPSHOT or BOOK_DELTA metadata")
        if self.first_sequence is not None and self.final_sequence is not None:
            if self.final_sequence < self.first_sequence:
                raise ValueError("final_sequence cannot precede first_sequence")


@dataclass(frozen=True, slots=True)
class PerpetualStateEvent:
    meta: MarketDataMeta
    mark_price: Decimal | None = None
    index_price: Decimal | None = None
    funding_rate: Decimal | None = None
    next_funding_time: datetime | None = None
    open_interest: Decimal | None = None
    open_interest_value: Decimal | None = None

    def __post_init__(self) -> None:
        if self.meta.kind != EventKind.PERPETUAL_STATE:
            raise ValueError("PerpetualStateEvent requires PERPETUAL_STATE metadata")
        if self.next_funding_time is not None:
            _assert_aware(self.next_funding_time, "next_funding_time")
        for name in ("mark_price", "index_price", "open_interest", "open_interest_value"):
            value = getattr(self, name)
            if value is not None and value < 0:
                raise ValueError(f"{name} cannot be negative")
        if all(
            value is None
            for value in (
                self.mark_price,
                self.index_price,
                self.funding_rate,
                self.next_funding_time,
                self.open_interest,
                self.open_interest_value,
            )
        ):
            raise ValueError("perpetual state event must contain at least one state field")


@dataclass(frozen=True, slots=True)
class LiquidationEvent:
    meta: MarketDataMeta
    liquidation_id: str | None
    liquidated_side: LiquidatedSide
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.meta.kind != EventKind.LIQUIDATION:
            raise ValueError("LiquidationEvent requires LIQUIDATION metadata")
        if self.price <= 0 or self.quantity <= 0:
            raise ValueError("liquidation price and quantity must be positive")



@dataclass(frozen=True, slots=True)
class PerpetualInstrumentDefinition:
    instrument_id: str
    venue: str
    venue_symbol: str
    base_asset: str
    quote_asset: str
    settle_asset: str
    contract_style: str
    tick_size: Decimal
    lot_size: Decimal
    contract_value: Decimal | None
    status: str
    effective_from: datetime
    published_at: datetime
    available_at: datetime
    received_at: datetime
    raw_sha256: str
    schema_version: str = "crypto-perps-instrument-v1"

    def __post_init__(self) -> None:
        for name in ("effective_from", "published_at", "available_at", "received_at"):
            _assert_aware(getattr(self, name), name)
        if self.contract_style not in {"LINEAR", "INVERSE"}:
            raise ValueError("contract_style must be LINEAR or INVERSE")
        if self.tick_size <= 0 or self.lot_size <= 0:
            raise ValueError("tick_size and lot_size must be positive")
        if self.contract_value is not None and self.contract_value <= 0:
            raise ValueError("contract_value must be positive when supplied")
        if self.published_at > self.available_at or self.available_at > self.received_at:
            raise ValueError("instrument publication/availability/receipt timestamps are inconsistent")
        if len(self.raw_sha256) != 64:
            raise ValueError("raw_sha256 must be a SHA-256 hex digest")

    def assert_usable_at(self, decision_time: datetime) -> None:
        _assert_aware(decision_time, "decision_time")
        if self.available_at > decision_time:
            raise ValueError("instrument definition was not available at decision time")


def payload_sha256(payload: bytes | str | dict[str, Any]) -> str:
    if isinstance(payload, dict):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    elif isinstance(payload, str):
        raw = payload.encode()
    else:
        raw = payload
    return sha256(raw).hexdigest()
