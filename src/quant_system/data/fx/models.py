from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class FxBar:
    pair: str
    base_currency: str
    quote_currency: str
    bar_start: datetime
    bar_end: datetime
    available_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    source: str
    raw_sha256: str

    def __post_init__(self) -> None:
        for name in ("bar_start", "bar_end", "available_at"):
            _aware(getattr(self, name), name)
        if self.bar_start >= self.bar_end:
            raise ValueError("bar_start must precede bar_end")
        if self.available_at < self.bar_end:
            raise ValueError("FX bar cannot be available before bar_end")
        pair = self.pair.replace("/", "").upper()
        if len(pair) != 6 or pair[:3] != self.base_currency.upper() or pair[3:] != self.quote_currency.upper():
            raise ValueError("pair must match base_currency/quote_currency")
        prices = (self.open, self.high, self.low, self.close)
        if any(p <= 0 for p in prices):
            raise ValueError("FX bar prices must be positive")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("FX bar high is inconsistent")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("FX bar low is inconsistent")
        if len(self.raw_sha256) != 64:
            raise ValueError("raw_sha256 must be SHA-256 hex")
        try:
            int(self.raw_sha256, 16)
        except ValueError as exc:
            raise ValueError("raw_sha256 must be SHA-256 hex") from exc

    @property
    def instrument_id(self) -> str:
        return f"FX:{self.base_currency.upper()}{self.quote_currency.upper()}"

    def canonical_identity(self) -> str:
        payload = {
            "pair": self.pair.replace("/", "").upper(),
            "base_currency": self.base_currency.upper(),
            "quote_currency": self.quote_currency.upper(),
            "bar_start": self.bar_start.isoformat(),
            "bar_end": self.bar_end.isoformat(),
            "available_at": self.available_at.isoformat(),
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
            "source": self.source,
            "raw_sha256": self.raw_sha256,
        }
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
