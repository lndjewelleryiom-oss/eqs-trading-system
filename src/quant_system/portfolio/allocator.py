from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_system.regime.models import MarketRegime


@dataclass(frozen=True, slots=True)
class StrategyAllocationInput:
    strategy_id: str
    base_weight: float
    confidence: float
    expected_volatility: float
    drawdown_fraction: float
    liquidity_score: float
    capacity_notional: Decimal
    regime_fit: dict[MarketRegime, float]
    degradation_multiplier: float = 1.0

    def __post_init__(self) -> None:
        if not 0 <= self.base_weight <= 1:
            raise ValueError("base_weight must be in [0, 1]")
        if not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be in [0, 1]")
        if self.expected_volatility <= 0:
            raise ValueError("expected_volatility must be positive")
        if not 0 <= self.drawdown_fraction <= 1:
            raise ValueError("drawdown_fraction must be in [0, 1]")
        if not 0 <= self.liquidity_score <= 1:
            raise ValueError("liquidity_score must be in [0, 1]")
        if self.capacity_notional < 0:
            raise ValueError("capacity_notional must be non-negative")
        if not 0 <= self.degradation_multiplier <= 1:
            raise ValueError("degradation_multiplier must be in [0, 1]")
        if any(not 0 <= fit <= 1 for fit in self.regime_fit.values()):
            raise ValueError("regime fitness must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class PortfolioConstraints:
    max_strategy_weight: float = 0.20
    max_gross_weight: float = 0.80
    min_cash_weight: float = 0.20
    target_strategy_volatility: float = 0.15
    min_liquidity_score: float = 0.25
    correlation_threshold: float = 0.80
    max_correlated_pair_weight: float = 0.25

    def __post_init__(self) -> None:
        for name in ("max_strategy_weight", "max_gross_weight", "min_cash_weight", "max_correlated_pair_weight"):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.max_gross_weight > 1 - self.min_cash_weight + 1e-12:
            raise ValueError("max_gross_weight conflicts with min_cash_weight")
        if self.target_strategy_volatility <= 0:
            raise ValueError("target_strategy_volatility must be positive")
        if not 0 <= self.min_liquidity_score <= 1:
            raise ValueError("min_liquidity_score must be in [0, 1]")
        if not 0 <= self.correlation_threshold <= 1:
            raise ValueError("correlation_threshold must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class AllocationResult:
    weights: dict[str, float]
    gross_weight: float
    cash_weight: float
    reasons: dict[str, tuple[str, ...]]


class ConstrainedPortfolioAllocator:
    """Conservative allocator that can only scale down each strategy's researched base weight."""

    def __init__(self, constraints: PortfolioConstraints = PortfolioConstraints()):
        self.constraints = constraints

    def allocate(
        self,
        candidates: list[StrategyAllocationInput],
        *,
        regime: MarketRegime,
        regime_confidence: float,
        nav: Decimal,
        correlations: dict[tuple[str, str], float] | None = None,
    ) -> AllocationResult:
        if nav <= 0:
            raise ValueError("nav must be positive")
        if not 0 <= regime_confidence <= 1:
            raise ValueError("regime_confidence must be in [0, 1]")
        ids = [candidate.strategy_id for candidate in candidates]
        if len(set(ids)) != len(ids):
            raise ValueError("strategy_id values must be unique")
        correlations = correlations or {}

        weights: dict[str, float] = {}
        reasons: dict[str, list[str]] = {candidate.strategy_id: [] for candidate in candidates}
        # Confidence in the market-state estimate never increases risk. At zero confidence,
        # retain at most 25% of the researched base allocation instead of guessing.
        regime_certainty_multiplier = 0.25 + 0.75 * regime_confidence

        for candidate in candidates:
            sid = candidate.strategy_id
            if candidate.liquidity_score < self.constraints.min_liquidity_score:
                weights[sid] = 0.0
                reasons[sid].append("LIQUIDITY_BELOW_MINIMUM")
                continue
            fit = candidate.regime_fit.get(regime, 0.0)
            vol_multiplier = min(1.0, self.constraints.target_strategy_volatility / candidate.expected_volatility)
            drawdown_multiplier = max(0.0, 1.0 - candidate.drawdown_fraction)
            adjusted = (
                candidate.base_weight
                * candidate.confidence
                * fit
                * candidate.liquidity_score
                * candidate.degradation_multiplier
                * vol_multiplier
                * drawdown_multiplier
                * regime_certainty_multiplier
            )
            adjusted = min(adjusted, candidate.base_weight, self.constraints.max_strategy_weight)
            capacity_weight = float(candidate.capacity_notional / nav)
            if adjusted > capacity_weight:
                adjusted = max(0.0, capacity_weight)
                reasons[sid].append("CAPACITY_CAPPED")
            if fit < 1.0:
                reasons[sid].append("REGIME_SCALING")
            if candidate.degradation_multiplier < 1.0:
                reasons[sid].append("DEGRADATION_SCALING")
            if vol_multiplier < 1.0:
                reasons[sid].append("VOLATILITY_SCALING")
            weights[sid] = adjusted

        self._enforce_correlations(weights, correlations, reasons)
        self._enforce_gross(weights, reasons)
        gross = sum(weights.values())
        return AllocationResult(
            dict(sorted(weights.items())),
            gross,
            max(0.0, 1.0 - gross),
            {sid: tuple(values) for sid, values in sorted(reasons.items())},
        )

    def _enforce_correlations(
        self,
        weights: dict[str, float],
        correlations: dict[tuple[str, str], float],
        reasons: dict[str, list[str]],
    ) -> None:
        for (left, right), correlation in sorted(correlations.items()):
            if left == right or left not in weights or right not in weights:
                continue
            if abs(correlation) < self.constraints.correlation_threshold:
                continue
            pair_weight = weights[left] + weights[right]
            if pair_weight <= self.constraints.max_correlated_pair_weight or pair_weight <= 0:
                continue
            scale = self.constraints.max_correlated_pair_weight / pair_weight
            weights[left] *= scale
            weights[right] *= scale
            reasons[left].append(f"CORRELATION_CAP:{right}")
            reasons[right].append(f"CORRELATION_CAP:{left}")

    def _enforce_gross(self, weights: dict[str, float], reasons: dict[str, list[str]]) -> None:
        gross = sum(weights.values())
        if gross <= self.constraints.max_gross_weight or gross <= 0:
            return
        scale = self.constraints.max_gross_weight / gross
        for sid in weights:
            weights[sid] *= scale
            reasons[sid].append("GROSS_CAP")
