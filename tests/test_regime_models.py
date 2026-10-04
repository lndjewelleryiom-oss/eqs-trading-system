from quant_system.regime import (
    GaussianRegimeClassifier,
    MarketRegime,
    RegimeFeatures,
    TransparentRegimeBaseline,
)


def f(trend, vol, mr, spread, liquidity, corr, risk_return):
    return RegimeFeatures(trend, vol, mr, spread, liquidity, corr, risk_return)


def test_transparent_baseline_detects_crisis_before_other_rules():
    assessment = TransparentRegimeBaseline().classify(f(0.8, 0.5, 0.1, 40, 0.1, 0.9, -0.12))
    assert assessment.regime == MarketRegime.CRISIS
    assert assessment.confidence == 0.95
    assert "HIGH_CORRELATION" in assessment.reasons


def test_transparent_baseline_classifies_trend_and_mean_reversion():
    baseline = TransparentRegimeBaseline()
    assert baseline.classify(f(0.8, 0.2, 0.1, 5, 0.8, 0.3, 0.01)).regime == MarketRegime.TRENDING
    assert baseline.classify(f(0.2, 0.2, 0.8, 5, 0.8, 0.3, 0.01)).regime == MarketRegime.MEAN_REVERTING


def test_gaussian_classifier_is_deterministic_and_probabilities_sum_to_one():
    samples = [
        (f(0.85, 0.18, 0.10, 4, 0.9, 0.25, 0.03), MarketRegime.TRENDING),
        (f(0.80, 0.20, 0.12, 5, 0.85, 0.30, 0.025), MarketRegime.TRENDING),
        (f(0.10, 0.45, 0.15, 35, 0.2, 0.88, -0.10), MarketRegime.CRISIS),
        (f(0.12, 0.50, 0.10, 40, 0.15, 0.92, -0.12), MarketRegime.CRISIS),
    ]
    model = GaussianRegimeClassifier().fit(samples)
    query = f(0.82, 0.19, 0.11, 5, 0.88, 0.27, 0.03)
    first = model.predict(query)
    second = model.predict(query)
    assert first == second
    assert first.regime == MarketRegime.TRENDING
    assert abs(sum(first.probabilities.values()) - 1.0) < 1e-12


def test_gaussian_classifier_requires_multiple_classes():
    model = GaussianRegimeClassifier()
    try:
        model.fit([(f(0.5, 0.2, 0.2, 5, 0.8, 0.3, 0.01), MarketRegime.TRENDING)])
    except ValueError as exc:
        assert "two" in str(exc)
    else:
        raise AssertionError("expected fit to fail")
