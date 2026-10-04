import random

import pytest

from quant_system.validation.statistics import (
    deflated_sharpe_ratio,
    holm_bonferroni,
    probability_of_backtest_overfitting,
    reality_check,
)


def test_deflated_sharpe_penalises_many_trials_and_noise():
    rng = random.Random(7)
    edge = [0.002 + rng.gauss(0, 0.001) for _ in range(240)]
    noise = [rng.gauss(0, 0.01) for _ in range(240)]
    edge_result = deflated_sharpe_ratio(edge, trials=20)
    noise_result = deflated_sharpe_ratio(noise, trials=100)
    assert edge_result.probability > 0.99
    assert noise_result.probability < 0.10
    assert deflated_sharpe_ratio(edge, trials=200).benchmark_sharpe > edge_result.benchmark_sharpe


def test_pbo_recovers_stable_candidate_and_flags_slice_mining():
    rng = random.Random(3)
    stable = []
    for _ in range(240):
        stable.append([
            0.0015 + rng.gauss(0, 0.0015),
            rng.gauss(0, 0.003),
            rng.gauss(0, 0.003),
            rng.gauss(0, 0.003),
        ])
    assert probability_of_backtest_overfitting(stable, n_slices=8).probability_of_backtest_overfitting == 0

    mined = []
    for i in range(160):
        active_slice = i // 20
        mined.append([
            (0.02 if candidate == active_slice else -0.003) + (0.001 if i % 2 == 0 else -0.001)
            for candidate in range(8)
        ])
    assert probability_of_backtest_overfitting(mined, n_slices=8).probability_of_backtest_overfitting == 1


def test_reality_check_distinguishes_clear_edge_from_null():
    rng = random.Random(19)
    candidates = {
        "edge": [0.002 + rng.gauss(0, 0.0015) for _ in range(180)],
        "noise1": [rng.gauss(0, 0.002) for _ in range(180)],
        "noise2": [rng.gauss(0, 0.002) for _ in range(180)],
    }
    result = reality_check(candidates, bootstrap_samples=500, seed=4, block_size=3)
    assert result.p_value < 0.05

    null = {
        "flat1": [0.0, 0.001, -0.001] * 60,
        "flat2": [0.0, -0.001, 0.001] * 60,
    }
    assert reality_check(null, bootstrap_samples=200, seed=4).p_value == 1.0


def test_holm_bonferroni_stops_after_first_non_rejection():
    decisions = holm_bonferroni({"a": 0.001, "b": 0.03, "c": 0.04}, alpha=0.05)
    assert decisions == {"a": True, "b": False, "c": False}


def test_pbo_rejects_odd_number_of_slices():
    with pytest.raises(ValueError, match="even"):
        probability_of_backtest_overfitting([[1, 2]] * 8, n_slices=5)
