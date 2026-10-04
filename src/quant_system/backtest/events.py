from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BarEvent:
    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("bar timestamp must be timezone-aware")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("prices must be positive")
        if self.volume < 0:
            raise ValueError("volume must be non-negative")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high is inconsistent")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low is inconsistent")


@dataclass(frozen=True, slots=True)
class SplitEvent:
    symbol: str
    timestamp: datetime
    ratio: Decimal  # new shares per old share; e.g. 2 for a 2-for-1 split

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("split timestamp must be timezone-aware")
        if self.ratio <= 0:
            raise ValueError("split ratio must be positive")


@dataclass(frozen=True, slots=True)
class CashDividendEvent:
    symbol: str
    timestamp: datetime
    amount_per_share: Decimal

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("dividend timestamp must be timezone-aware")
        if self.amount_per_share < 0:
            raise ValueError("cash dividend must be non-negative")
