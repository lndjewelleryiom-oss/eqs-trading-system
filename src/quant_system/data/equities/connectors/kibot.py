from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal
import csv
import http.cookiejar
import io
import urllib.parse
import urllib.request
from typing import Mapping

from quant_system.data.raw_store import ImmutableRawStore, StoredBlob

from ..models import EquityBarEvent, EquityEventKind, EquityMarketDataMeta
from .base import UnknownEquityInstrumentError


@dataclass(frozen=True, slots=True)
class KibotGuestDailyReceipt:
    symbol: str
    source_symbol: str
    raw_blob: StoredBlob
    available_at: datetime
    received_at: datetime
    bars: tuple[EquityBarEvent, ...]
    transport: str = "HTTPS"
    source_classification: str = "PUBLIC_GUEST_EOD"


class KibotGuestDailySource:
    """Read-only public-guest EOD source for EQS-02 acceptance.

    Source publication timestamps are not provided in the CSV response, so
    published_at is conservatively set to the local byte-availability time.
    Historical bars therefore cannot be backdated into a decision that
    predates this collection event.
    """

    BASE_URL = "https://api.kibot.com/"
    VENUE = "KIBOT_GUEST_EOD"

    def __init__(
        self,
        instrument_map: Mapping[str, str],
        raw_store: ImmutableRawStore,
        *,
        timeout_seconds: int = 20,
    ):
        self._instrument_map = {k.upper(): v for k, v in instrument_map.items()}
        self.raw_store = raw_store
        self.timeout_seconds = timeout_seconds

    def instrument_id(self, symbol: str) -> str:
        try:
            return self._instrument_map[symbol.upper()]
        except KeyError as exc:
            raise UnknownEquityInstrumentError(
                f"Kibot symbol {symbol!r} is absent from the point-in-time instrument map"
            ) from exc

    @staticmethod
    def _opener():
        jar = http.cookiejar.CookieJar()
        return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def _login(self, opener) -> None:
        query = urllib.parse.urlencode({"action": "login", "user": "guest", "password": "guest"})
        req = urllib.request.Request(
            self.BASE_URL + "?" + query,
            headers={"User-Agent": "EQS-Research/1.0"},
        )
        with opener.open(req, timeout=self.timeout_seconds) as response:
            payload = response.read()
            service_ok = payload.startswith(b"200 OK") or (
                payload.startswith(b"407 Already logged in") and b"as 'guest'" in payload
            )
            if response.status != 200 or not service_ok:
                raise RuntimeError("KIBOT_GUEST_LOGIN_FAILED")

    def _download(self, opener, symbol: str, period: int) -> bytes:
        if period < 1 or period > 1000:
            raise ValueError("period must be between 1 and 1000")
        query = urllib.parse.urlencode(
            {"action": "history", "symbol": symbol.upper(), "interval": "daily", "period": period}
        )
        req = urllib.request.Request(
            self.BASE_URL + "?" + query,
            headers={"User-Agent": "EQS-Research/1.0"},
        )
        with opener.open(req, timeout=self.timeout_seconds) as response:
            raw = response.read()
            if response.status != 200:
                raise RuntimeError(f"KIBOT_HISTORY_HTTP_{response.status}")
        return raw

    def collect(self, symbol: str, *, period: int = 10) -> KibotGuestDailyReceipt:
        canonical_id = self.instrument_id(symbol)
        opener = self._opener()
        self._login(opener)
        raw = self._download(opener, symbol, period)
        available_at = datetime.now(timezone.utc)
        blob = self.raw_store.put(raw)
        received_at = datetime.now(timezone.utc)
        bars = self.parse(
            raw,
            symbol=symbol,
            instrument_id=canonical_id,
            raw_sha256=blob.content_hash,
            available_at=available_at,
            received_at=received_at,
        )
        return KibotGuestDailyReceipt(
            symbol=symbol.upper(),
            source_symbol=symbol.upper(),
            raw_blob=blob,
            available_at=available_at,
            received_at=received_at,
            bars=bars,
        )

    @classmethod
    def parse(
        cls,
        raw: bytes,
        *,
        symbol: str,
        instrument_id: str,
        raw_sha256: str,
        available_at: datetime,
        received_at: datetime,
    ) -> tuple[EquityBarEvent, ...]:
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        if received_at.tzinfo is None or received_at.utcoffset() is None:
            raise ValueError("received_at must be timezone-aware")
        if available_at > received_at:
            raise ValueError("available_at cannot be after received_at")

        text = raw.decode("utf-8-sig").strip()
        if not text:
            raise ValueError("KIBOT_EMPTY_RESPONSE")
        rows = list(csv.reader(io.StringIO(text)))
        bars: list[EquityBarEvent] = []
        for index, row in enumerate(rows, 1):
            if len(row) != 6:
                raise ValueError(f"KIBOT_ROW_FIELD_COUNT:{index}")
            date_s, open_s, high_s, low_s, close_s, volume_s = (x.strip() for x in row)
            try:
                session_date = datetime.strptime(date_s, "%m/%d/%Y").date()
                open_px = Decimal(open_s)
                high_px = Decimal(high_s)
                low_px = Decimal(low_s)
                close_px = Decimal(close_s)
                volume = Decimal(volume_s)
            except Exception as exc:
                raise ValueError(f"KIBOT_ROW_PARSE_FAILED:{index}") from exc

            bar_start = datetime.combine(session_date, time.min, tzinfo=timezone.utc)
            bar_end = datetime.combine(session_date, time.max, tzinfo=timezone.utc)
            if bar_end > available_at:
                raise ValueError(f"KIBOT_INCOMPLETE_OR_FUTURE_SESSION:{date_s}")

            meta = EquityMarketDataMeta(
                venue=cls.VENUE,
                instrument_id=instrument_id,
                venue_symbol=symbol.upper(),
                kind=EquityEventKind.BAR,
                event_time=bar_end,
                published_at=available_at,
                available_at=available_at,
                received_at=received_at,
                source_channel="guest-history-daily",
                source_sequence=date_s,
                raw_sha256=raw_sha256,
            )
            bars.append(
                EquityBarEvent(
                    meta=meta,
                    bar_start=bar_start,
                    bar_end=bar_end,
                    open=open_px,
                    high=high_px,
                    low=low_px,
                    close=close_px,
                    volume=volume,
                )
            )
        if not bars:
            raise ValueError("KIBOT_NO_BARS")
        if len({bar.bar_start.date() for bar in bars}) != len(bars):
            raise ValueError("KIBOT_DUPLICATE_SESSION_DATE")
        if any(a.bar_start >= b.bar_start for a, b in zip(bars, bars[1:])):
            raise ValueError("KIBOT_NON_MONOTONIC_SESSION_ORDER")
        return tuple(bars)
