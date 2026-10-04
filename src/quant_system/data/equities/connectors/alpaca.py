from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from typing import Any, Mapping
import urllib.parse
import urllib.request

from .base import EquityInstrumentMappedConnector, UnsupportedEquityMessageError
from ..models import EquityBarEvent, EquityEventKind, EquityMarketDataMeta


@dataclass(frozen=True, slots=True)
class AlpacaMarketDataResponse:
    raw: bytes
    received_at: datetime
    http_status: int
    url: str


class AlpacaIexLatestBarConnector(EquityInstrumentMappedConnector):
    """Authenticated Alpaca market-data connector for US Stocks/ETFs.

    This module has no trading endpoint and no order-submission capability.
    Credentials are accepted at construction time and are used only as HTTP
    headers against data.alpaca.markets.
    """

    venue = "ALPACA_IEX"

    def __init__(
        self,
        instrument_map: Mapping[str, str],
        *,
        api_key_id: str,
        api_secret_key: str,
        base_url: str = "https://data.alpaca.markets",
    ) -> None:
        super().__init__(instrument_map)
        if not api_key_id or not api_secret_key:
            raise ValueError("Alpaca API credentials are required")
        if base_url.rstrip("/") != "https://data.alpaca.markets":
            raise ValueError("only Alpaca market-data base URL is permitted")
        self._api_key_id = api_key_id
        self._api_secret_key = api_secret_key
        self._base_url = base_url.rstrip("/")

    def fetch_latest_bars(self, symbols: list[str], *, timeout_seconds: int = 20) -> AlpacaMarketDataResponse:
        if not symbols:
            raise ValueError("at least one symbol is required")
        for symbol in symbols:
            self.instrument_id(symbol)
        query = urllib.parse.urlencode({
            "symbols": ",".join(symbols),
            "feed": "iex",
        })
        url = f"{self._base_url}/v2/stocks/bars/latest?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": self._api_key_id,
                "APCA-API-SECRET-KEY": self._api_secret_key,
                "Accept": "application/json",
                "User-Agent": "EQS/eqs02-paper",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
            status = int(response.status)
        return AlpacaMarketDataResponse(
            raw=raw,
            received_at=datetime.now(timezone.utc),
            http_status=status,
            url=url,
        )

    def normalize_latest_bars(
        self,
        payload: dict[str, Any],
        *,
        available_at: datetime,
        received_at: datetime,
        raw_sha256: str,
    ) -> tuple[EquityBarEvent, ...]:
        self.assert_ingestion_clock(available_at, received_at)
        bars = payload.get("bars")
        if not isinstance(bars, dict) or not bars:
            raise UnsupportedEquityMessageError("Alpaca latest-bars payload missing bars")

        out: list[EquityBarEvent] = []
        for symbol, row in sorted(bars.items()):
            if not isinstance(row, dict):
                raise UnsupportedEquityMessageError("Alpaca bar row must be an object")
            instrument_id = self.instrument_id(symbol)
            try:
                start = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00")).astimezone(timezone.utc)
                open_ = Decimal(str(row["o"]))
                high = Decimal(str(row["h"]))
                low = Decimal(str(row["l"]))
                close = Decimal(str(row["c"]))
                volume = Decimal(str(row.get("v", 0)))
            except (KeyError, ValueError, TypeError) as exc:
                raise UnsupportedEquityMessageError(f"invalid Alpaca bar for {symbol}") from exc

            end = start + timedelta(minutes=1)
            if end > available_at:
                raise UnsupportedEquityMessageError(
                    f"Alpaca bar for {symbol} is not closed at availability time"
                )

            meta = EquityMarketDataMeta(
                venue=self.venue,
                instrument_id=instrument_id,
                venue_symbol=symbol,
                kind=EquityEventKind.BAR,
                event_time=end,
                published_at=end,
                available_at=available_at,
                received_at=received_at,
                source_channel="stocks:latest_bar:iex",
                source_sequence=str(row.get("n", "")),
                raw_sha256=raw_sha256,
            )
            out.append(
                EquityBarEvent(
                    meta=meta,
                    bar_start=start,
                    bar_end=end,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                )
            )
        return tuple(out)


    def fetch_bars(
        self,
        symbols: list[str],
        *,
        start: datetime,
        end: datetime,
        timeframe: str = "1Min",
        limit: int = 1000,
        timeout_seconds: int = 20,
    ) -> AlpacaMarketDataResponse:
        self.assert_ingestion_clock(start, end)
        if not symbols:
            raise ValueError("at least one symbol is required")
        if timeframe != "1Min":
            raise ValueError("EQS-02 forward acceptance currently permits 1Min bars only")
        if not 1 <= limit <= 10000:
            raise ValueError("limit must be between 1 and 10000")
        for symbol in symbols:
            self.instrument_id(symbol)
        query = urllib.parse.urlencode({
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "limit": str(limit),
            "feed": "iex",
            "sort": "asc",
        })
        url = f"{self._base_url}/v2/stocks/bars?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "APCA-API-KEY-ID": self._api_key_id,
                "APCA-API-SECRET-KEY": self._api_secret_key,
                "Accept": "application/json",
                "User-Agent": "EQS/eqs02-paper",
            },
            method="GET",
        )
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read()
            status = int(response.status)
        return AlpacaMarketDataResponse(
            raw=raw,
            received_at=datetime.now(timezone.utc),
            http_status=status,
            url=url,
        )

    def normalize_bars(
        self,
        payload: dict[str, Any],
        *,
        available_at: datetime,
        received_at: datetime,
        raw_sha256: str,
    ) -> dict[str, tuple[EquityBarEvent, ...]]:
        self.assert_ingestion_clock(available_at, received_at)
        bars = payload.get("bars")
        if not isinstance(bars, dict) or not bars:
            raise UnsupportedEquityMessageError("Alpaca bars payload missing bars")
        out: dict[str, tuple[EquityBarEvent, ...]] = {}
        for symbol, rows in sorted(bars.items()):
            if not isinstance(rows, list) or not rows:
                raise UnsupportedEquityMessageError(f"Alpaca bars for {symbol} must be a non-empty list")
            instrument_id = self.instrument_id(symbol)
            events: list[EquityBarEvent] = []
            previous_start: datetime | None = None
            for row in rows:
                if not isinstance(row, dict):
                    raise UnsupportedEquityMessageError("Alpaca bar row must be an object")
                try:
                    start = datetime.fromisoformat(str(row["t"]).replace("Z", "+00:00")).astimezone(timezone.utc)
                    open_ = Decimal(str(row["o"]))
                    high = Decimal(str(row["h"]))
                    low = Decimal(str(row["l"]))
                    close = Decimal(str(row["c"]))
                    volume = Decimal(str(row.get("v", 0)))
                except (KeyError, ValueError, TypeError) as exc:
                    raise UnsupportedEquityMessageError(f"invalid Alpaca bar for {symbol}") from exc
                if previous_start is not None and start <= previous_start:
                    raise UnsupportedEquityMessageError(f"Alpaca bars for {symbol} are not strictly increasing")
                previous_start = start
                bar_end = start + timedelta(minutes=1)
                if bar_end > available_at:
                    raise UnsupportedEquityMessageError(f"Alpaca bar for {symbol} is not closed at availability time")
                meta = EquityMarketDataMeta(
                    venue=self.venue,
                    instrument_id=instrument_id,
                    venue_symbol=symbol,
                    kind=EquityEventKind.BAR,
                    event_time=bar_end,
                    published_at=bar_end,
                    available_at=available_at,
                    received_at=received_at,
                    source_channel="stocks:bars:1Min:iex",
                    source_sequence=str(row.get("n", "")),
                    raw_sha256=raw_sha256,
                )
                events.append(
                    EquityBarEvent(
                        meta=meta,
                        bar_start=start,
                        bar_end=bar_end,
                        open=open_,
                        high=high,
                        low=low,
                        close=close,
                        volume=volume,
                    )
                )
            out[symbol] = tuple(events)
        return out

    @staticmethod
    def decode_json(raw: bytes) -> dict[str, Any]:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise UnsupportedEquityMessageError("Alpaca response root must be an object")
        return value
