from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .base import InstrumentMappedConnector, StreamSubscription, UnsupportedMessageError, ms_timestamp, sequence_string
from ..models import (
    BookLevel,
    BookUpdate,
    EventKind,
    LiquidatedSide,
    LiquidationEvent,
    MarketDataMeta,
    PerpetualStateEvent,
    TradeEvent,
    payload_sha256,
)


class BinanceUsdmConnector(InstrumentMappedConnector):
    """Normalizer for Binance USDⓈ-M public perpetual-futures streams.

    Network I/O is intentionally outside this class. The collector timestamps the socket
    receive boundary, persists raw bytes, then passes decoded payloads here.
    """

    venue = "BINANCE_USDM"
    PUBLIC_ENDPOINT = "wss://fstream.binance.com/public"
    MARKET_ENDPOINT = "wss://fstream.binance.com/market"

    def __init__(self, instrument_map: Mapping[str, str]):
        super().__init__(instrument_map)

    def subscriptions(self, venue_symbol: str) -> tuple[StreamSubscription, ...]:
        symbol = venue_symbol.lower()
        self.instrument_id(venue_symbol)
        return (
            StreamSubscription(self.venue, self.PUBLIC_ENDPOINT, "public", f"{symbol}@depth@100ms", venue_symbol),
            StreamSubscription(self.venue, self.MARKET_ENDPOINT, "market", f"{symbol}@aggTrade", venue_symbol),
            StreamSubscription(self.venue, self.MARKET_ENDPOINT, "market", f"{symbol}@markPrice@1s", venue_symbol),
            StreamSubscription(self.venue, self.MARKET_ENDPOINT, "market", f"{symbol}@forceOrder", venue_symbol),
        )

    def normalize(
        self,
        payload: dict[str, Any],
        *,
        available_at: datetime,
        received_at: datetime,
        raw_sha256: str | None = None,
    ) -> tuple[TradeEvent | BookUpdate | PerpetualStateEvent | LiquidationEvent, ...]:
        self.assert_ingestion_clock(available_at, received_at)
        raw_hash = raw_sha256 or payload_sha256(payload)
        raw = payload.get("data", payload)
        event_type = raw.get("e")
        if event_type == "aggTrade":
            return (self._trade(raw, payload, available_at, received_at, raw_hash),)
        if event_type == "depthUpdate":
            return (self._book(raw, payload, available_at, received_at, raw_hash),)
        if event_type == "markPriceUpdate":
            return (self._state(raw, payload, available_at, received_at, raw_hash),)
        if event_type == "forceOrder":
            return (self._liquidation(raw, payload, available_at, received_at, raw_hash),)
        raise UnsupportedMessageError(f"unsupported Binance USD-M event type: {event_type!r}")

    def _meta(self, *, raw: dict[str, Any], whole: dict[str, Any], kind: EventKind,
              event_ms: int | str, publish_ms: int | str, channel: str,
              sequence: str | None, available_at: datetime, received_at: datetime, raw_hash: str) -> MarketDataMeta:
        symbol = raw.get("s") or raw.get("o", {}).get("s")
        return MarketDataMeta(
            venue=self.venue,
            instrument_id=self.instrument_id(symbol),
            venue_symbol=symbol,
            kind=kind,
            event_time=ms_timestamp(event_ms),
            published_at=ms_timestamp(publish_ms),
            available_at=available_at,
            received_at=received_at,
            source_channel=channel,
            source_sequence=sequence,
            raw_sha256=raw_hash,
        )

    def _trade(self, raw: dict[str, Any], whole: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> TradeEvent:
        meta = self._meta(
            raw=raw, whole=whole, kind=EventKind.TRADE, event_ms=raw["T"], publish_ms=raw["E"],
            channel="aggTrade", sequence=sequence_string(raw.get("a")), available_at=available_at, received_at=received_at, raw_hash=raw_hash,
        )
        # Binance m=true means the buyer is maker, therefore the aggressor was SELL.
        aggressor = "SELL" if bool(raw.get("m")) else "BUY"
        return TradeEvent(meta, str(raw["a"]), Decimal(raw["p"]), Decimal(raw["q"]), aggressor)

    def _book(self, raw: dict[str, Any], whole: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> BookUpdate:
        event_ms = raw.get("T", raw["E"])
        meta = self._meta(
            raw=raw, whole=whole, kind=EventKind.BOOK_DELTA, event_ms=event_ms, publish_ms=raw["E"],
            channel="depth", sequence=sequence_string(raw.get("U"), raw.get("u")), available_at=available_at, received_at=received_at, raw_hash=raw_hash,
        )
        return BookUpdate(
            meta=meta,
            bids=tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in raw.get("b", ())),
            asks=tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in raw.get("a", ())),
            first_sequence=int(raw["U"]),
            final_sequence=int(raw["u"]),
            previous_sequence=int(raw["pu"]) if raw.get("pu") is not None else None,
        )

    def _state(self, raw: dict[str, Any], whole: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> PerpetualStateEvent:
        meta = self._meta(
            raw=raw, whole=whole, kind=EventKind.PERPETUAL_STATE, event_ms=raw["E"], publish_ms=raw["E"],
            channel="markPrice", sequence=None, available_at=available_at, received_at=received_at, raw_hash=raw_hash,
        )
        next_funding = ms_timestamp(raw["T"]) if raw.get("T") else None
        return PerpetualStateEvent(
            meta=meta,
            mark_price=Decimal(raw["p"]) if raw.get("p") not in (None, "") else None,
            index_price=Decimal(raw["i"]) if raw.get("i") not in (None, "") else None,
            funding_rate=Decimal(raw["r"]) if raw.get("r") not in (None, "") else None,
            next_funding_time=next_funding,
        )

    def _liquidation(self, raw: dict[str, Any], whole: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> LiquidationEvent:
        order = raw["o"]
        meta_raw = {**raw, "s": order["s"]}
        event_ms = order.get("T", raw["E"])
        meta = self._meta(
            raw=meta_raw, whole=whole, kind=EventKind.LIQUIDATION, event_ms=event_ms, publish_ms=raw["E"],
            channel="forceOrder", sequence=sequence_string(order.get("T")), available_at=available_at, received_at=received_at, raw_hash=raw_hash,
        )
        # Forced SELL closes a long; forced BUY closes a short.
        side = LiquidatedSide.LONG if order["S"].upper() == "SELL" else LiquidatedSide.SHORT
        quantity = Decimal(order.get("z") or order["q"])
        price = Decimal(order.get("ap") or order.get("p"))
        return LiquidationEvent(meta, None, side, price, quantity)
