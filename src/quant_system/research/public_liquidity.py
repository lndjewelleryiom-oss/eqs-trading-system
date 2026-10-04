from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import statistics
import time
from typing import Callable
import urllib.parse
import urllib.request


class PublicLiquidityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DailyQuoteVolume:
    venue: str
    symbol: str
    day: date
    quote_volume_usd: Decimal


@dataclass(frozen=True, slots=True)
class LiquidityEligibilityPoint:
    venue: str
    symbol: str
    day: date
    history_days: int
    rolling_30d_median_quote_volume_usd: Decimal | None
    eligible_10m: bool
    eligible_25m: bool


def _get_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(
        url, headers={"User-Agent": "EQS-R13-Public-Liquidity/1.0"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def _millis(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp() * 1000)


class PublicDailyLiquidityClient:
    """Read-only historical daily volume client for the three frozen crypto venues."""

    def __init__(
        self,
        *,
        requester: Callable[[str], object] | None = None,
        retry_count: int = 4,
        retry_sleep_seconds: float = 0.25,
    ):
        self.requester = requester or _get_json
        self.retry_count = retry_count
        self.retry_sleep_seconds = retry_sleep_seconds

    def _request(self, url: str):
        error = None
        for attempt in range(self.retry_count):
            try:
                return self.requester(url)
            except Exception as exc:
                error = exc
                time.sleep(self.retry_sleep_seconds * (attempt + 1))
        raise PublicLiquidityError(f"request failed after retries: {url}: {error}")

    def fetch(
        self,
        *,
        venue: str,
        symbol: str,
        start: date,
        end: date,
    ) -> tuple[DailyQuoteVolume, ...]:
        if end < start:
            raise ValueError("end must be >= start")
        if venue == "BINANCE_USDM":
            return self._fetch_binance(symbol, start, end)
        if venue == "BYBIT_LINEAR":
            return self._fetch_bybit(symbol, start, end)
        if venue == "OKX_SWAP":
            return self._fetch_okx(symbol, start, end)
        raise PublicLiquidityError(f"unsupported venue: {venue}")

    def _fetch_binance(self, symbol: str, start: date, end: date):
        rows: dict[date, DailyQuoteVolume] = {}
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=1498))
            params = urllib.parse.urlencode(
                {
                    "symbol": symbol,
                    "interval": "1d",
                    "startTime": _millis(cursor),
                    "endTime": _millis(chunk_end + timedelta(days=1)) - 1,
                    "limit": 1500,
                }
            )
            data = self._request(f"https://fapi.binance.com/fapi/v1/klines?{params}")
            if not isinstance(data, list):
                raise PublicLiquidityError("Binance kline response must be a list")
            for item in data:
                if not isinstance(item, list) or len(item) < 8:
                    raise PublicLiquidityError("Binance kline row malformed")
                day = datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc).date()
                rows[day] = DailyQuoteVolume(
                    "BINANCE_USDM", symbol, day, Decimal(str(item[7]))
                )
            cursor = chunk_end + timedelta(days=1)
        return tuple(rows[k] for k in sorted(rows) if start <= k <= end)

    def _fetch_bybit(self, symbol: str, start: date, end: date):
        rows: dict[date, DailyQuoteVolume] = {}
        cursor = start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=998))
            params = urllib.parse.urlencode(
                {
                    "category": "linear",
                    "symbol": symbol,
                    "interval": "D",
                    "start": _millis(cursor),
                    "end": _millis(chunk_end + timedelta(days=1)) - 1,
                    "limit": 1000,
                }
            )
            data = self._request(f"https://api.bybit.com/v5/market/kline?{params}")
            if not isinstance(data, dict) or data.get("retCode") != 0:
                raise PublicLiquidityError(f"Bybit response error: {data}")
            raw = ((data.get("result") or {}).get("list") or [])
            for item in raw:
                if not isinstance(item, list) or len(item) < 7:
                    raise PublicLiquidityError("Bybit kline row malformed")
                day = datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc).date()
                rows[day] = DailyQuoteVolume(
                    "BYBIT_LINEAR", symbol, day, Decimal(str(item[6]))
                )
            cursor = chunk_end + timedelta(days=1)
        return tuple(rows[k] for k in sorted(rows) if start <= k <= end)

    def _fetch_okx(self, symbol: str, start: date, end: date):
        rows: dict[date, DailyQuoteVolume] = {}
        # OKX history-candles returns newest first and accepts at most 300 rows.
        cursor_end = end + timedelta(days=1)
        while cursor_end > start:
            cursor_start = max(start, cursor_end - timedelta(days=299))
            params = urllib.parse.urlencode(
                {
                    "instId": symbol,
                    "bar": "1Dutc",
                    "after": _millis(cursor_end),
                    # OKX treats before/after boundaries exclusively.
                    # Widen the lower edge by one day, then filter locally.
                    "before": _millis(cursor_start - timedelta(days=1)),
                    "limit": 300,
                }
            )
            data = self._request(
                f"https://www.okx.com/api/v5/market/history-candles?{params}"
            )
            if not isinstance(data, dict) or data.get("code") != "0":
                raise PublicLiquidityError(f"OKX response error: {data}")
            raw = data.get("data") or []
            for item in raw:
                if not isinstance(item, list) or len(item) < 8:
                    raise PublicLiquidityError("OKX candle row malformed")
                day = datetime.fromtimestamp(int(item[0]) / 1000, tz=timezone.utc).date()
                rows[day] = DailyQuoteVolume(
                    "OKX_SWAP", symbol, day, Decimal(str(item[7]))
                )
            if not raw:
                break
            cursor_end = cursor_start
        return tuple(rows[k] for k in sorted(rows) if start <= k <= end)


def eligibility_series(
    rows: tuple[DailyQuoteVolume, ...],
    *,
    available_since: date,
    available_to: date | None = None,
    minimum_history_days: int = 90,
    window_days: int = 30,
) -> tuple[LiquidityEligibilityPoint, ...]:
    ordered = sorted(rows, key=lambda x: x.day)
    by_day = {row.day: row for row in ordered}
    result = []
    for row in ordered:
        if row.day < available_since:
            continue
        if available_to is not None and row.day > available_to:
            continue
        history_days = max((row.day - available_since).days, 0)
        window_start = row.day - timedelta(days=window_days - 1)
        values = [
            by_day[window_start + timedelta(days=i)].quote_volume_usd
            for i in range(window_days)
            if window_start + timedelta(days=i) in by_day
        ]
        median = (
            Decimal(str(statistics.median(values)))
            if len(values) == window_days
            else None
        )
        result.append(
            LiquidityEligibilityPoint(
                venue=row.venue,
                symbol=row.symbol,
                day=row.day,
                history_days=history_days,
                rolling_30d_median_quote_volume_usd=median,
                eligible_10m=bool(
                    history_days >= minimum_history_days
                    and median is not None
                    and median >= Decimal("10000000")
                ),
                eligible_25m=bool(
                    history_days >= minimum_history_days
                    and median is not None
                    and median >= Decimal("25000000")
                ),
            )
        )
    return tuple(result)
