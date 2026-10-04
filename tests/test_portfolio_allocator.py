from decimal import Decimal

from quant_system.portfolio import (
    ConstrainedPortfolioAllocator,
    PortfolioConstraints,
    StrategyAllocationInput,
    correlation_matrix,
)
from quant_system.regime import MarketRegime


def candidate(strategy_id: str, **overrides):
    values = dict(
        strategy_id=strategy_id,
        base_weight=0.20,
        confidence=1.0,
        expected_volatility=0.10,
        drawdown_fraction=0.0,
        liquidity_score=1.0,
        capacity_notional=Decimal("1000000"),
        regime_fit={MarketRegime.TRENDING: 1.0, MarketRegime.CRISIS: 0.2},
        degradation_multiplier=1.0,
    )
    values.update(overrides)
    return StrategyAllocationInput(**values)


def test_regime_change_reduces_allocation_and_respects_limits():
    allocator = ConstrainedPortfolioAllocator(PortfolioConstraints(max_strategy_weight=0.20, max_gross_weight=0.60, min_cash_weight=0.40))
    candidates = [candidate("trend-a"), candidate("trend-b"), candidate("trend-c")]
    normal = allocator.allocate(candidates, regime=MarketRegime.TRENDING, regime_confidence=1.0, nav=Decimal("1000000"))
    crisis = allocator.allocate(candidates, regime=MarketRegime.CRISIS, regime_confidence=1.0, nav=Decimal("1000000"))
    assert normal.gross_weight <= 0.60 + 1e-12
    assert crisis.gross_weight < normal.gross_weight
    assert crisis.cash_weight >= 0.40
    assert all(weight <= 0.20 + 1e-12 for weight in crisis.weights.values())


def test_capacity_and_liquidity_caps_are_fail_closed():
    allocator = ConstrainedPortfolioAllocator()
    low_liquidity = candidate("illiquid", liquidity_score=0.1)
    capacity_limited = candidate("small", capacity_notional=Decimal("50000"))
    result = allocator.allocate([low_liquidity, capacity_limited], regime=MarketRegime.TRENDING, regime_confidence=1.0, nav=Decimal("1000000"))
    assert result.weights["illiquid"] == 0.0
    assert result.weights["small"] <= 0.05 + 1e-12
    assert "LIQUIDITY_BELOW_MINIMUM" in result.reasons["illiquid"]
    assert "CAPACITY_CAPPED" in result.reasons["small"]


def test_correlation_pair_cap_reduces_combined_exposure():
    constraints = PortfolioConstraints(max_correlated_pair_weight=0.15)
    allocator = ConstrainedPortfolioAllocator(constraints)
    result = allocator.allocate(
        [candidate("a"), candidate("b")],
        regime=MarketRegime.TRENDING,
        regime_confidence=1.0,
        nav=Decimal("1000000"),
        correlations={("a", "b"): 0.95},
    )
    assert result.weights["a"] + result.weights["b"] <= 0.15 + 1e-12
    assert "CORRELATION_CAP:b" in result.reasons["a"]


def test_degradation_can_only_reduce_researched_base_weight():
    allocator = ConstrainedPortfolioAllocator()
    healthy = allocator.allocate([candidate("a")], regime=MarketRegime.TRENDING, regime_confidence=1.0, nav=Decimal("1000000"))
    degraded = allocator.allocate([candidate("a", degradation_multiplier=0.4)], regime=MarketRegime.TRENDING, regime_confidence=1.0, nav=Decimal("1000000"))
    assert degraded.weights["a"] < healthy.weights["a"]
    assert degraded.weights["a"] <= 0.20


def test_correlation_matrix_detects_positive_and_negative_relationships():
    matrix = correlation_matrix({"a": [1, 2, 3, 4], "b": [2, 4, 6, 8], "c": [8, 6, 4, 2]})
    assert abs(matrix[("a", "b")] - 1.0) < 1e-12
    assert abs(matrix[("a", "c")] + 1.0) < 1e-12
