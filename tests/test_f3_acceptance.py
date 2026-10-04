"""Phase F3 exit acceptance using synthetic fixtures only.

No result in this file is evidence of a tradable market edge. The fixtures are
constructed to prove that the validation machinery can reject obvious selection
bias and recover a deliberately embedded statistical edge.
"""

import random
from statistics import mean

from quant_system.validation.gates import assess_candidate
from quant_system.validation.sensitivity import parameter_surface, profitable_fraction
from quant_system.validation.splits import holdout_split, walk_forward_splits
from quant_system.validation.statistics import (
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    reality_check,
)


def test_controlled_synthetic_edge_passes_validation_gate():
    rng = random.Random(101)
    # Stable synthetic return stream with deliberately embedded positive mean.
    returns = [0.0018 + rng.gauss(0, 0.0015) for _ in range(320)]
    holdout = holdout_split(len(returns), test_size=80, gap=5)
    oos_mean = mean(returns[i] for i in holdout.test_indices)

    folds = walk_forward_splits(len(returns), train_size=120, test_size=40, step=40, gap=5)
    wf_scores = [mean(returns[i] for i in fold.test_indices) for fold in folds]
    wf_positive = sum(score > 0 for score in wf_scores) / len(wf_scores)

    # Broad synthetic parameter plateau; not a needle optimum.
    surface = parameter_surface(
        {"lookback": [10, 20, 30], "threshold": [0.8, 1.0, 1.2]},
        lambda p: 0.0015 - abs(p["lookback"] - 20) * 0.00001 - abs(p["threshold"] - 1.0) * 0.0002,
    )

    matrix_rng = random.Random(202)
    matrix = [
        [
            0.0016 + matrix_rng.gauss(0, 0.0015),
            matrix_rng.gauss(0, 0.003),
            matrix_rng.gauss(0, 0.003),
            matrix_rng.gauss(0, 0.003),
        ]
        for _ in range(240)
    ]
    pbo = probability_of_backtest_overfitting(matrix, n_slices=8)
    candidate_series = {f"c{col}": [row[col] for row in matrix] for col in range(4)}
    rc = reality_check(candidate_series, bootstrap_samples=500, seed=303, block_size=3)
    dsr = deflated_sharpe_ratio(returns, trials=25)

    decision = assess_candidate(
        out_of_sample_profitable_after_costs=oos_mean > 0,
        walk_forward_positive_fraction=wf_positive,
        parameter_profitable_fraction=profitable_fraction(surface),
        cost_stress_survived=oos_mean - 0.0005 > 0,
        dsr_probability=dsr.probability,
        pbo=pbo.probability_of_backtest_overfitting,
        reality_check_p=rc.p_value,
        leakage_checks_passed=True,
    )
    assert decision.passed, decision.reasons


def test_deliberately_overfit_fixture_is_rejected():
    # Each candidate only wins in the slice that selected it; CSCV should expose
    # that the IS winner collapses out of sample.
    matrix = []
    for i in range(160):
        active_slice = i // 20
        matrix.append([
            (0.02 if candidate == active_slice else -0.003) + (0.001 if i % 2 == 0 else -0.001)
            for candidate in range(8)
        ])
    pbo = probability_of_backtest_overfitting(matrix, n_slices=8)

    decision = assess_candidate(
        out_of_sample_profitable_after_costs=False,
        walk_forward_positive_fraction=0.25,
        parameter_profitable_fraction=0.125,
        cost_stress_survived=False,
        dsr_probability=0.60,
        pbo=pbo.probability_of_backtest_overfitting,
        reality_check_p=0.40,
        leakage_checks_passed=True,
    )
    assert pbo.probability_of_backtest_overfitting == 1.0
    assert not decision.passed
    assert "PBO exceeds threshold" in decision.reasons
