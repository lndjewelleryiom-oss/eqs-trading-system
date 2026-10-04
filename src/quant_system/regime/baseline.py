from __future__ import annotations

from dataclasses import dataclass

from .models import MarketRegime, RegimeAssessment, RegimeFeatures


@dataclass(frozen=True, slots=True)
class BaselineThresholds:
    high_volatility: float = 0.30
    low_volatility: float = 0.10
    strong_trend: float = 0.65
    strong_mean_reversion: float = 0.65
    wide_spread_bps: float = 25.0
    low_liquidity: float = 0.30
    high_correlation: float = 0.75
    severe_risk_asset_loss: float = -0.05
    risk_on_return: float = 0.02


class TransparentRegimeBaseline:
    """Rule-based baseline whose output can be explained without model introspection."""

    def __init__(self, thresholds: BaselineThresholds = BaselineThresholds()):
        self.thresholds = thresholds

    def classify(self, f: RegimeFeatures) -> RegimeAssessment:
        t = self.thresholds
        reasons: list[str] = []

        if (
            f.realized_volatility >= t.high_volatility
            and f.cross_asset_correlation >= t.high_correlation
            and f.risk_asset_return <= t.severe_risk_asset_loss
        ):
            reasons.extend(("HIGH_VOLATILITY", "HIGH_CORRELATION", "RISK_ASSET_DRAWDOWN"))
            return self._assessment(MarketRegime.CRISIS, 0.95, reasons)
        if f.spread_bps >= t.wide_spread_bps or f.liquidity_score <= t.low_liquidity:
            reasons.append("LIQUIDITY_STRESS")
            return self._assessment(MarketRegime.LIQUIDITY_CONTRACTION, 0.85, reasons)
        if f.realized_volatility >= t.high_volatility:
            reasons.append("HIGH_VOLATILITY")
            return self._assessment(MarketRegime.HIGH_VOL, 0.80, reasons)
        if f.realized_volatility <= t.low_volatility and f.liquidity_score >= 0.70:
            reasons.extend(("LOW_VOLATILITY", "HEALTHY_LIQUIDITY"))
            return self._assessment(MarketRegime.LOW_VOL, 0.75, reasons)
        if f.trend_strength >= t.strong_trend:
            reasons.append("STRONG_TREND")
            return self._assessment(MarketRegime.TRENDING, 0.75, reasons)
        if f.mean_reversion_score >= t.strong_mean_reversion:
            reasons.append("STRONG_MEAN_REVERSION")
            return self._assessment(MarketRegime.MEAN_REVERTING, 0.75, reasons)
        if f.risk_asset_return >= t.risk_on_return and f.cross_asset_correlation < t.high_correlation:
            reasons.append("POSITIVE_RISK_ASSET_RETURN")
            return self._assessment(MarketRegime.RISK_ON, 0.65, reasons)
        if f.risk_asset_return < 0:
            reasons.append("NEGATIVE_RISK_ASSET_RETURN")
            return self._assessment(MarketRegime.RISK_OFF, 0.60, reasons)
        return self._assessment(MarketRegime.UNKNOWN, 0.0, ("NO_RULE_DOMINATES",))

    @staticmethod
    def _assessment(regime: MarketRegime, confidence: float, reasons: tuple[str, ...] | list[str]) -> RegimeAssessment:
        return RegimeAssessment(regime, confidence, {regime: 1.0}, tuple(reasons))
