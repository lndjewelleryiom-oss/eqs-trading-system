from quant_system.monitoring import (
    DegradationMonitor,
    DegradationState,
    ExpectedBehavior,
    ObservedBehavior,
    population_stability_index,
)


EXPECTED = ExpectedBehavior(
    win_rate=0.55,
    expectancy=0.002,
    sharpe=1.5,
    max_drawdown=0.10,
    slippage_bps=5.0,
    trades_per_period=100,
)


def observed(**overrides):
    values = dict(win_rate=0.54, expectancy=0.0019, sharpe=1.45, drawdown=0.08, slippage_bps=5.2, trades_per_period=98)
    values.update(overrides)
    return ObservedBehavior(**values)


def test_healthy_behavior_stays_active():
    result = DegradationMonitor().assess(EXPECTED, observed())
    assert result.state == DegradationState.ACTIVE
    assert result.allocation_multiplier == 1.0


def test_moderate_deterioration_reduces_allocation():
    result = DegradationMonitor().assess(EXPECTED, observed(sharpe=0.9, expectancy=0.0012))
    assert result.state == DegradationState.REDUCED
    assert result.allocation_multiplier == 0.40


def test_drawdown_or_integrity_failure_pauses_strategy():
    monitor = DegradationMonitor()
    drawdown = monitor.assess(EXPECTED, observed(drawdown=0.13))
    integrity = monitor.assess(EXPECTED, observed(), data_integrity_error=True)
    assert drawdown.state == DegradationState.PAUSED
    assert integrity.state == DegradationState.PAUSED
    assert integrity.allocation_multiplier == 0.0


def test_distribution_drift_can_reduce_or_pause():
    monitor = DegradationMonitor()
    reduced = monitor.assess(EXPECTED, observed(), feature_psi=0.25)
    paused = monitor.assess(EXPECTED, observed(), prediction_psi=0.40)
    assert reduced.state == DegradationState.REDUCED
    assert paused.state == DegradationState.PAUSED


def test_population_stability_index_detects_shift():
    expected = [float(i) for i in range(100)]
    same = [float(i) for i in range(100)]
    shifted = [float(i + 100) for i in range(100)]
    assert population_stability_index(expected, same) == 0.0
    assert population_stability_index(expected, shifted) > 0.35
