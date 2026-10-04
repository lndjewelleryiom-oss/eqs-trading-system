from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import lzma
import struct
import urllib.request

from .models import SpotCommodityBar


@dataclass(frozen=True, slots=True)
class SpotCommodityTick:
    event_time: datetime
    bid: Decimal
    ask: Decimal

    @property
    def mid(self) -> Decimal:
        return (self.bid+self.ask)/Decimal("2")


class DukascopySpotCommoditySource:
    def __init__(
        self,
        *,
        symbol: str,
        underlying: str,
        quote_currency: str,
        unit: str,
        price_divisor: Decimal,
    ) -> None:
        if price_divisor<=0:
            raise ValueError("price_divisor must be positive")
        self.symbol=symbol.replace("/","").upper()
        self.underlying=underlying.upper()
        self.quote_currency=quote_currency.upper()
        self.unit=unit
        self.price_divisor=price_divisor
        if self.symbol != self.underlying+self.quote_currency:
            raise ValueError("symbol must match underlying/quote_currency")

    def daily_url(self, day: date) -> str:
        return (
            f"https://www.dukascopy.com/datafeed/{self.symbol}/"
            f"{day.year:04d}/{day.month-1:02d}/{day.day:02d}_ticks.bi5"
        )

    def fetch(self, day: date, *, timeout_seconds: int=45) -> tuple[bytes,int,str]:
        url=self.daily_url(day)
        req=urllib.request.Request(url,headers={"User-Agent":"Mozilla/5.0","Accept":"*/*"})
        with urllib.request.urlopen(req,timeout=timeout_seconds) as response:
            raw=response.read()
            status=int(response.status)
        return raw,status,url

    def decode_ticks(self, raw: bytes, day: date) -> tuple[SpotCommodityTick,...]:
        dec=lzma.decompress(raw)
        if len(dec)%20:
            raise ValueError("Dukascopy BI5 record alignment invalid")
        base=datetime(day.year,day.month,day.day,tzinfo=timezone.utc)
        ticks=[]
        prev_ms=None
        for i in range(0,len(dec),20):
            ms,ask_i,bid_i,_ask_vol,_bid_vol=struct.unpack(">IIIff",dec[i:i+20])
            if prev_ms is not None and ms<prev_ms:
                raise ValueError("Dukascopy ticks are not monotonic")
            prev_ms=ms
            ask=Decimal(ask_i)/self.price_divisor
            bid=Decimal(bid_i)/self.price_divisor
            if bid<=0 or ask<=0 or bid>ask:
                raise ValueError("invalid or crossed Dukascopy spot quote")
            ticks.append(SpotCommodityTick(base+timedelta(milliseconds=ms),bid,ask))
        return tuple(ticks)

    def aggregate_bars(
        self,
        ticks: tuple[SpotCommodityTick,...],
        *,
        raw_sha256: str,
        available_at: datetime,
        minutes: int=5,
        only_after: datetime | None=None,
    ) -> tuple[SpotCommodityBar,...]:
        if available_at.tzinfo is None or available_at.utcoffset() is None:
            raise ValueError("available_at must be timezone-aware")
        if minutes<=0 or 60%minutes:
            raise ValueError("minutes must be a positive divisor of 60")
        buckets=defaultdict(list)
        for tick in ticks:
            if only_after is not None and tick.event_time<=only_after:
                continue
            minute=(tick.event_time.minute//minutes)*minutes
            start=tick.event_time.replace(minute=minute,second=0,microsecond=0)
            end=start+timedelta(minutes=minutes)
            if end<=available_at:
                buckets[start].append(tick.mid)
        bars=[]
        for start in sorted(buckets):
            prices=buckets[start]
            bars.append(SpotCommodityBar(
                symbol=self.symbol,
                underlying=self.underlying,
                quote_currency=self.quote_currency,
                unit=self.unit,
                bar_start=start,
                bar_end=start+timedelta(minutes=minutes),
                available_at=available_at,
                open=prices[0],
                high=max(prices),
                low=min(prices),
                close=prices[-1],
                source="DUKASCOPY_PUBLIC_DAILY_TICK",
                raw_sha256=raw_sha256,
            ))
        return tuple(bars)

    @staticmethod
    def sha256(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()
