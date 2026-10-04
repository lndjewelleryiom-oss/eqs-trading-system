"""F4 phase exit: a synthetic regime shock must de-risk without violating limits."""

from decimal import Decimal

from quant_system.monitoring import DegradationMonitor, ExpectedBehavior, ObservedBehavior
from quant_system.portfolio import ConstrainedPortfolioAllocator, PortfolioConstraints, StrategyAllocationInput
from quant_system.regime import MarketRegime, RegimeFeatures, TransparentRegimeBaseline


def test_regime_shock_and_degradation_reduce_allocation_without_limit_breach():
    baseline = TransparentRegimeBaseline()
    allocator = ConstrainedPortfolioAllocator(
        PortfolioConstraints(max_strategy_weight=0.18, max_gross_weight=0.50, min_cash_weight=0.50, max_correlated_pair_weight=0.25)
    )
    expected = ExpectedBehavior(0.55, 0.002, 1.5, 0.10, 5.0, 100)
    healthy_obs = ObservedBehavior(0.54, 0.0019, 1.45, 0.08, 5.2, 98)
    stressed_obs = ObservedBehavior(0.50, 0.0011, 0.85, 0.11, 7.5, 75)
    monitor = DegradationMonitor()
    healthy_multiplier = monitor.assess(expected, healthy_obs).allocation_multiplier
    stressed_multiplier = monitor.assess(expected, stressed_obs).allocation_multiplier

    inputs = [
        StrategyAllocationInput(
            strategy_id=f"s{i}",
            base_weight=0.18,
            confidence=0.95,
            expected_volatility=0.12,
            drawdown_fraction=0.03,
            liquidity_score=0.9,
            capacity_notional=Decimal("500000"),
            regime_fit={MarketRegime.TRENDING: 1.0, MarketRegime.CRISIS: 0.15},
            degradation_multiplier=healthy_multiplier,
        )
        for i in range(3)
    ]
    normal_assessment = baseline.classify(RegimeFeatures(0.8, 0.18, 0.1, 5, 0.9, 0.3, 0.03))
    normal = allocator.allocate(
        inputs,
        regime=normal_assessment.regime,
        regime_confidence=normal_assessment.confidence,
        nav=Decimal("1000000"),
        correlations={("s0", "s1"): 0.90},
    )

    crisis_assessment = baseline.classify(RegimeFeatures(0.2, 0.55, 0.1, 45, 0.1, 0.9, -0.12))
    stressed_inputs = [
        StrategyAllocationInput(
            strategy_id=item.strategy_id,
            base_weight=item.base_weight,
            confidence=item.confidence,
            expected_volatility=0.30,
            drawdown_fraction=0.08,
            liquidity_score=0.7,
            capacity_notional=item.capacity_notional,
            regime_fit=item.regime_fit,
            degradation_multiplier=stressed_multiplier,
        )
        for item in inputs
    ]
    crisis = allocator.allocate(
        stressed_inputs,
        regime=crisis_assessment.regime,
        regime_confidence=crisis_assessment.confidence,
        nav=Decimal("1000000"),
        correlations={("s0", "s1"): 0.95},
    )

    assert crisis_assessment.regime == MarketRegime.CRISIS
    assert crisis.gross_weight < normal.gross_weight
    assert crisis.gross_weight <= 0.50 + 1e-12
    assert crisis.cash_weight >= 0.50 - 1e-12
    assert all(weight <= 0.18 + 1e-12 for weight in crisis.weights.values())
    assert crisis.weights["s0"] + crisis.weights["s1"] <= 0.25 + 1e-12
