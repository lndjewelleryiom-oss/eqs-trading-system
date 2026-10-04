from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MarketRegime(StrEnum):
    TRENDING = "TRENDING"
    MEAN_REVERTING = "MEAN_REVERTING"
    HIGH_VOL = "HIGH_VOL"
    LOW_VOL = "LOW_VOL"
    LIQUIDITY_CONTRACTION = "LIQUIDITY_CONTRACTION"
    LIQUIDITY_EXPANSION = "LIQUIDITY_EXPANSION"
    RISK_ON = "RISK_ON"
    RISK_OFF = "RISK_OFF"
    CRISIS = "CRISIS"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class RegimeFeatures:
    trend_strength: float
    realized_volatility: float
    mean_reversion_score: float
    spread_bps: float
    liquidity_score: float
    cross_asset_correlation: float
    risk_asset_return: float

    def as_tuple(self) -> tuple[float, ...]:
        return (
            self.trend_strength,
            self.realized_volatility,
            self.mean_reversion_score,
            self.spread_bps,
            self.liquidity_score,
            self.cross_asset_correlation,
            self.risk_asset_return,
        )


@dataclass(frozen=True, slots=True)
class RegimeAssessment:
    regime: MarketRegime
    confidence: float
    probabilities: dict[MarketRegime, float]
    reasons: tuple[str, ...]
