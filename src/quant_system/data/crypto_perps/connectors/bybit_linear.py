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


class BybitLinearConnector(InstrumentMappedConnector):
    venue = "BYBIT_LINEAR"
    PUBLIC_ENDPOINT = "wss://stream.bybit.com/v5/public/linear"

    def __init__(self, instrument_map: Mapping[str, str]):
        super().__init__(instrument_map)

    def subscriptions(self, venue_symbol: str) -> tuple[StreamSubscription, ...]:
        self.instrument_id(venue_symbol)
        channels = (
            f"orderbook.50.{venue_symbol}",
            f"publicTrade.{venue_symbol}",
            f"tickers.{venue_symbol}",
            f"allLiquidation.{venue_symbol}",
        )
        return tuple(
            StreamSubscription(
                self.venue,
                self.PUBLIC_ENDPOINT,
                "public",
                channel,
                venue_symbol,
                subscribe_payload={"op": "subscribe", "args": [channel]},
            )
            for channel in channels
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
        topic = str(payload.get("topic", ""))
        if topic.startswith("orderbook."):
            return (self._book(payload, available_at, received_at, raw_hash),)
        if topic.startswith("publicTrade."):
            return self._trades(payload, available_at, received_at, raw_hash)
        if topic.startswith("tickers."):
            return (self._state(payload, available_at, received_at, raw_hash),)
        if topic.startswith("allLiquidation."):
            return self._liquidations(payload, available_at, received_at, raw_hash)
        raise UnsupportedMessageError(f"unsupported Bybit linear topic: {topic!r}")

    def _meta(self, *, payload: dict[str, Any], symbol: str, kind: EventKind,
              event_ms: int | str, channel: str, sequence: str | None,
              available_at: datetime, received_at: datetime, raw_hash: str) -> MarketDataMeta:
        return MarketDataMeta(
            venue=self.venue,
            instrument_id=self.instrument_id(symbol),
            venue_symbol=symbol,
            kind=kind,
            event_time=ms_timestamp(event_ms),
            published_at=ms_timestamp(payload["ts"]),
            available_at=available_at,
            received_at=received_at,
            source_channel=channel,
            source_sequence=sequence,
            raw_sha256=raw_hash,
        )

    def _book(self, payload: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> BookUpdate:
        data = payload["data"]
        kind = EventKind.BOOK_SNAPSHOT if payload.get("type") == "snapshot" else EventKind.BOOK_DELTA
        event_ms = data.get("cts", payload["ts"])
        meta = self._meta(
            payload=payload, symbol=data["s"], kind=kind, event_ms=event_ms,
            channel="orderbook", sequence=sequence_string(data.get("u"), data.get("seq")),
            available_at=available_at, received_at=received_at, raw_hash=raw_hash,
        )
        return BookUpdate(
            meta,
            tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in data.get("b", ())),
            tuple(BookLevel(Decimal(p), Decimal(q)) for p, q in data.get("a", ())),
            final_sequence=int(data["u"]) if data.get("u") is not None else None,
        )

    def _trades(self, payload: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> tuple[TradeEvent, ...]:
        events: list[TradeEvent] = []
        for item in payload.get("data", ()):  # Bybit may batch multiple trades in one push.
            meta = self._meta(
                payload=payload, symbol=item["s"], kind=EventKind.TRADE, event_ms=item["T"],
                channel="publicTrade", sequence=sequence_string(item.get("seq"), item.get("i")),
                available_at=available_at, received_at=received_at, raw_hash=raw_hash,
            )
            side = item.get("S", "").upper() or None
            events.append(TradeEvent(meta, str(item["i"]), Decimal(item["p"]), Decimal(item["v"]), side))
        return tuple(events)

    def _state(self, payload: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> PerpetualStateEvent:
        data = payload["data"]
        meta = self._meta(
            payload=payload, symbol=data["symbol"], kind=EventKind.PERPETUAL_STATE,
            event_ms=payload["ts"], channel="tickers", sequence=sequence_string(payload.get("cs")),
            available_at=available_at, received_at=received_at, raw_hash=raw_hash,
        )
        def dec(name: str) -> Decimal | None:
            value = data.get(name)
            return Decimal(value) if value not in (None, "") else None
        return PerpetualStateEvent(
            meta,
            mark_price=dec("markPrice"),
            index_price=dec("indexPrice"),
            funding_rate=dec("fundingRate"),
            next_funding_time=ms_timestamp(data["nextFundingTime"]) if data.get("nextFundingTime") else None,
            open_interest=dec("openInterest"),
            open_interest_value=dec("openInterestValue"),
        )

    def _liquidations(self, payload: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> tuple[LiquidationEvent, ...]:
        events: list[LiquidationEvent] = []
        for item in payload.get("data", ()):
            meta = self._meta(
                payload=payload, symbol=item["s"], kind=EventKind.LIQUIDATION, event_ms=item["T"],
                channel="allLiquidation", sequence=None, available_at=available_at, received_at=received_at, raw_hash=raw_hash,
            )
            # Bybit documents Buy as a liquidated long and Sell as a liquidated short.
            side = LiquidatedSide.LONG if item["S"].upper() == "BUY" else LiquidatedSide.SHORT
            events.append(LiquidationEvent(meta, None, side, Decimal(item["p"]), Decimal(item["v"])))
        return tuple(events)
