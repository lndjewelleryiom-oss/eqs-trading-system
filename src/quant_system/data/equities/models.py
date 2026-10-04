from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any


class EquityAssetType(StrEnum):
    STOCK = "STOCK"
    ETF = "ETF"


class EquityEventKind(StrEnum):
    TRADE = "TRADE"
    QUOTE = "QUOTE"
    BAR = "BAR"
    CORPORATE_ACTION = "CORPORATE_ACTION"


class CorporateActionKind(StrEnum):
    CASH_DIVIDEND = "CASH_DIVIDEND"
    STOCK_SPLIT = "STOCK_SPLIT"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"


def _assert_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _assert_sha256(value: str, name: str = "raw_sha256") -> None:
    if len(value) != 64:
        raise ValueError(f"{name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValueError(f"{name} must be a SHA-256 hex digest") from exc


@dataclass(frozen=True, slots=True)
class EquityMarketDataMeta:
    venue: str
    instrument_id: str
    venue_symbol: str
    kind: EquityEventKind
    event_time: datetime
    published_at: datetime
    available_at: datetime
    received_at: datetime
    source_channel: str
    source_sequence: str | None
    raw_sha256: str
    schema_version: str = "equity-etf-v1"

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
        _assert_sha256(self.raw_sha256)

    def assert_usable_at(self, decision_time: datetime) -> None:
        _assert_aware(decision_time, "decision_time")
        if self.available_at > decision_time:
            raise ValueError("equity market-data event was not available at decision time")

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
class EquityInstrumentDefinition:
    instrument_id: str
    venue: str
    venue_symbol: str
    asset_type: EquityAssetType
    primary_exchange: str
    quote_currency: str
    tick_size: Decimal
    lot_size: Decimal
    status: str
    effective_from: datetime
    published_at: datetime
    available_at: datetime
    received_at: datetime
    raw_sha256: str
    schema_version: str = "equity-etf-instrument-v1"

    def __post_init__(self) -> None:
        for name in ("effective_from", "published_at", "available_at", "received_at"):
            _assert_aware(getattr(self, name), name)
        if not self.instrument_id or not self.venue or not self.venue_symbol or not self.primary_exchange:
            raise ValueError("instrument identity fields are required")
        if not self.quote_currency:
            raise ValueError("quote_currency is required")
        if self.tick_size <= 0 or self.lot_size <= 0:
            raise ValueError("tick_size and lot_size must be positive")
        if self.published_at > self.available_at or self.available_at > self.received_at:
            raise ValueError("instrument publication/availability/receipt timestamps are inconsistent")
        _assert_sha256(self.raw_sha256)

    def assert_usable_at(self, decision_time: datetime) -> None:
        _assert_aware(decision_time, "decision_time")
        if self.available_at > decision_time:
            raise ValueError("instrument definition was not available at decision time")


@dataclass(frozen=True, slots=True)
class EquityTradeEvent:
    meta: EquityMarketDataMeta
    trade_id: str
    price: Decimal
    quantity: Decimal

    def __post_init__(self) -> None:
        if self.meta.kind != EquityEventKind.TRADE:
            raise ValueError("EquityTradeEvent requires TRADE metadata")
        if not self.trade_id:
            raise ValueError("trade_id is required")
        if self.price <= 0 or self.quantity <= 0:
            raise ValueError("trade price and quantity must be positive")


@dataclass(frozen=True, slots=True)
class EquityQuoteEvent:
    meta: EquityMarketDataMeta
    bid_price: Decimal
    ask_price: Decimal
    bid_size: Decimal
    ask_size: Decimal

    def __post_init__(self) -> None:
        if self.meta.kind != EquityEventKind.QUOTE:
            raise ValueError("EquityQuoteEvent requires QUOTE metadata")
        if self.bid_price <= 0 or self.ask_price <= 0:
            raise ValueError("quote prices must be positive")
        if self.bid_size < 0 or self.ask_size < 0:
            raise ValueError("quote sizes cannot be negative")
        if self.bid_price > self.ask_price:
            raise ValueError("crossed quote is invalid")


@dataclass(frozen=True, slots=True)
class EquityBarEvent:
    meta: EquityMarketDataMeta
    bar_start: datetime
    bar_end: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.meta.kind != EquityEventKind.BAR:
            raise ValueError("EquityBarEvent requires BAR metadata")
        _assert_aware(self.bar_start, "bar_start")
        _assert_aware(self.bar_end, "bar_end")
        if self.bar_start >= self.bar_end:
            raise ValueError("bar_start must precede bar_end")
        if self.bar_end > self.meta.event_time:
            raise ValueError("bar_end cannot be after event_time")
        prices = (self.open, self.high, self.low, self.close)
        if any(p <= 0 for p in prices):
            raise ValueError("bar prices must be positive")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low is inconsistent")
        if self.volume < 0:
            raise ValueError("bar volume cannot be negative")


@dataclass(frozen=True, slots=True)
class CorporateActionEvent:
    meta: EquityMarketDataMeta
    action_id: str
    action_kind: CorporateActionKind
    effective_at: datetime
    cash_amount: Decimal | None = None
    split_ratio: Decimal | None = None
    old_symbol: str | None = None
    new_symbol: str | None = None

    def __post_init__(self) -> None:
        if self.meta.kind != EquityEventKind.CORPORATE_ACTION:
            raise ValueError("CorporateActionEvent requires CORPORATE_ACTION metadata")
        if not self.action_id:
            raise ValueError("action_id is required")
        _assert_aware(self.effective_at, "effective_at")
        if self.action_kind == CorporateActionKind.CASH_DIVIDEND:
            if self.cash_amount is None or self.cash_amount < 0:
                raise ValueError("cash dividend requires non-negative cash_amount")
        elif self.action_kind == CorporateActionKind.STOCK_SPLIT:
            if self.split_ratio is None or self.split_ratio <= 0:
                raise ValueError("stock split requires positive split_ratio")
        elif self.action_kind == CorporateActionKind.SYMBOL_CHANGE:
            if not self.old_symbol or not self.new_symbol or self.old_symbol == self.new_symbol:
                raise ValueError("symbol change requires distinct old_symbol and new_symbol")


def payload_sha256(payload: bytes | str | dict[str, Any]) -> str:
    if isinstance(payload, dict):
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    elif isinstance(payload, str):
        raw = payload.encode()
    else:
        raw = payload
    return sha256(raw).hexdigest()
