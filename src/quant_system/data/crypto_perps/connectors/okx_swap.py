from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping

from .base import InstrumentMappedConnector, StreamSubscription, UnsupportedMessageError, ms_timestamp, sequence_string
from ..models import BookLevel, BookUpdate, EventKind, MarketDataMeta, PerpetualStateEvent, TradeEvent, payload_sha256


class OkxSwapConnector(InstrumentMappedConnector):
    venue = "OKX_SWAP"
    PUBLIC_ENDPOINT = "wss://ws.okx.com:8443/ws/v5/public"

    def __init__(self, instrument_map: Mapping[str, str]):
        super().__init__(instrument_map)

    def subscriptions(self, venue_symbol: str) -> tuple[StreamSubscription, ...]:
        self.instrument_id(venue_symbol)
        channels = ("books", "trades", "mark-price", "funding-rate", "open-interest")
        return tuple(
            StreamSubscription(
                self.venue,
                self.PUBLIC_ENDPOINT,
                "public",
                channel,
                venue_symbol,
                subscribe_payload={"op": "subscribe", "args": [{"channel": channel, "instId": venue_symbol}]},
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
    ) -> tuple[TradeEvent | BookUpdate | PerpetualStateEvent, ...]:
        self.assert_ingestion_clock(available_at, received_at)
        raw_hash = raw_sha256 or payload_sha256(payload)
        channel = payload.get("arg", {}).get("channel")
        if channel == "books":
            return self._books(payload, available_at, received_at, raw_hash)
        if channel == "trades":
            return self._trades(payload, available_at, received_at, raw_hash)
        if channel in {"mark-price", "funding-rate", "open-interest"}:
            return self._states(payload, channel, available_at, received_at, raw_hash)
        raise UnsupportedMessageError(f"unsupported OKX SWAP channel: {channel!r}")

    def _meta(self, *, payload: dict[str, Any], symbol: str, kind: EventKind,
              event_ms: int | str, channel: str, sequence: str | None,
              available_at: datetime, received_at: datetime, raw_hash: str) -> MarketDataMeta:
        # OKX public payloads generally expose one venue timestamp `ts`; in v1 it is used
        # for both event and publication time, while local socket receipt defines availability.
        return MarketDataMeta(
            venue=self.venue,
            instrument_id=self.instrument_id(symbol),
            venue_symbol=symbol,
            kind=kind,
            event_time=ms_timestamp(event_ms),
            published_at=ms_timestamp(event_ms),
            available_at=available_at,
            received_at=received_at,
            source_channel=channel,
            source_sequence=sequence,
            raw_sha256=raw_hash,
        )

    def _books(self, payload: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> tuple[BookUpdate, ...]:
        symbol = payload["arg"]["instId"]
        kind = EventKind.BOOK_SNAPSHOT if payload.get("action") == "snapshot" else EventKind.BOOK_DELTA
        result: list[BookUpdate] = []
        for item in payload.get("data", ()):
            meta = self._meta(
                payload=payload, symbol=symbol, kind=kind, event_ms=item["ts"], channel="books",
                sequence=sequence_string(item.get("seqId")), available_at=available_at, received_at=received_at, raw_hash=raw_hash,
            )
            result.append(BookUpdate(
                meta,
                tuple(BookLevel(Decimal(row[0]), Decimal(row[1])) for row in item.get("bids", ())),
                tuple(BookLevel(Decimal(row[0]), Decimal(row[1])) for row in item.get("asks", ())),
                final_sequence=int(item["seqId"]) if item.get("seqId") is not None else None,
                previous_sequence=int(item["prevSeqId"]) if item.get("prevSeqId") is not None else None,
                checksum=int(item["checksum"]) if item.get("checksum") is not None else None,
            ))
        return tuple(result)

    def _trades(self, payload: dict[str, Any], available_at: datetime, received_at: datetime, raw_hash: str) -> tuple[TradeEvent, ...]:
        symbol = payload["arg"]["instId"]
        result: list[TradeEvent] = []
        for item in payload.get("data", ()):
            meta = self._meta(
                payload=payload, symbol=symbol, kind=EventKind.TRADE, event_ms=item["ts"], channel="trades",
                sequence=sequence_string(item.get("tradeId")), available_at=available_at, received_at=received_at, raw_hash=raw_hash,
            )
            result.append(TradeEvent(
                meta, str(item["tradeId"]), Decimal(item["px"]), Decimal(item["sz"]), item.get("side", "").upper() or None
            ))
        return tuple(result)

    def _states(self, payload: dict[str, Any], channel: str, available_at: datetime, received_at: datetime, raw_hash: str) -> tuple[PerpetualStateEvent, ...]:
        symbol = payload["arg"]["instId"]
        result: list[PerpetualStateEvent] = []
        for item in payload.get("data", ()):
            event_ms = item.get("ts") or item.get("fundingTime")
            meta = self._meta(
                payload=payload, symbol=symbol, kind=EventKind.PERPETUAL_STATE, event_ms=event_ms,
                channel=channel, sequence=None, available_at=available_at, received_at=received_at, raw_hash=raw_hash,
            )
            def dec(name: str) -> Decimal | None:
                value = item.get(name)
                return Decimal(value) if value not in (None, "") else None
            result.append(PerpetualStateEvent(
                meta,
                mark_price=dec("markPx"),
                funding_rate=dec("fundingRate"),
                next_funding_time=ms_timestamp(item["nextFundingTime"]) if item.get("nextFundingTime") else None,
                open_interest=dec("oi"),
                open_interest_value=dec("oiUsd"),
            ))
        return tuple(result)
