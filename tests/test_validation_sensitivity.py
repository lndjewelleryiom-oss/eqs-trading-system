import pytest

from quant_system.validation.sensitivity import cost_surface, parameter_surface, profitable_fraction


def test_parameter_surface_is_complete_and_deterministic():
    grid = {"lookback": [10, 20], "threshold": [1.0, 2.0, 3.0]}
    evaluator = lambda p: p["lookback"] / 10 - abs(p["threshold"] - 2)
    first = parameter_surface(grid, evaluator)
    second = parameter_surface({"threshold": [1.0, 2.0, 3.0], "lookback": [10, 20]}, evaluator)
    assert first == second
    assert len(first) == 6
    assert profitable_fraction(first) == pytest.approx(4 / 6)


def test_cost_surface_applies_conservative_multipliers():
    points = cost_surface(
        {"commission": 0.001, "slippage": 0.002},
        [1, 2, 3],
        lambda costs: 0.01 - sum(costs.values()),
    )
    assert [p.value for p in points] == pytest.approx([0.007, 0.004, 0.001])


def test_cost_surface_rejects_negative_multiplier():
    with pytest.raises(ValueError, match="non-negative"):
        cost_surface({"commission": 1}, [-1], lambda costs: 0)
