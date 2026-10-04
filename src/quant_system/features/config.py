from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json


@dataclass(frozen=True, slots=True)
class FeatureEngineConfig:
    engine_version: str = "crypto-perps-feature-engine-v1"
    feature_version: str = "1.0.0"
    microstructure_window_seconds: int = 60
    funding_window_seconds: int = 300
    basis_window_seconds: int = 300
    open_interest_window_seconds: int = 300
    liquidation_window_seconds: int = 60
    volatility_window_seconds: int = 300
    momentum_short_window_seconds: int = 60
    momentum_long_window_seconds: int = 300
    book_depth_levels: int = 5
    regime_wide_spread_bps: float = 25.0
    regime_high_volatility_bps: float = 200.0
    regime_low_volatility_bps: float = 35.0
    regime_trend_return_bps: float = 25.0
    regime_trend_efficiency: float = 0.65

    def __post_init__(self) -> None:
        windows = (
            self.microstructure_window_seconds,
            self.funding_window_seconds,
            self.basis_window_seconds,
            self.open_interest_window_seconds,
            self.liquidation_window_seconds,
            self.volatility_window_seconds,
            self.momentum_short_window_seconds,
            self.momentum_long_window_seconds,
        )
        if any(value <= 0 for value in windows):
            raise ValueError("feature windows must be positive")
        if self.book_depth_levels <= 0:
            raise ValueError("book_depth_levels must be positive")
        if self.momentum_short_window_seconds > self.momentum_long_window_seconds:
            raise ValueError("short momentum window cannot exceed long momentum window")
        if not 0 <= self.regime_trend_efficiency <= 1:
            raise ValueError("regime_trend_efficiency must be in [0, 1]")

    @property
    def fingerprint(self) -> str:
        payload = asdict(self)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()
