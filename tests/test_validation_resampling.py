import pytest

from quant_system.validation.resampling import bootstrap_confidence_interval, monte_carlo_trade_paths


def test_bootstrap_is_seed_reproducible_and_contains_obvious_mean():
    values = [0.009, 0.010, 0.011, 0.012, 0.008] * 20
    first = bootstrap_confidence_interval(values, simulations=400, seed=17, block_size=5)
    second = bootstrap_confidence_interval(values, simulations=400, seed=17, block_size=5)
    assert first == second
    assert first.lower <= first.estimate <= first.upper
    assert first.estimate == pytest.approx(0.01)


def test_monte_carlo_trade_paths_are_seed_reproducible():
    trades = [0.02, -0.01, 0.015, -0.005, 0.01] * 10
    first = monte_carlo_trade_paths(trades, simulations=300, seed=11)
    second = monte_carlo_trade_paths(trades, simulations=300, seed=11)
    assert first == second
    assert first.p05_terminal_equity <= first.median_terminal_equity <= first.p95_terminal_equity
    assert first.p95_max_drawdown >= first.median_max_drawdown >= 0


def test_monte_carlo_rejects_impossible_return():
    with pytest.raises(ValueError, match="-100%"):
        monte_carlo_trade_paths([0.1, -1.0])
