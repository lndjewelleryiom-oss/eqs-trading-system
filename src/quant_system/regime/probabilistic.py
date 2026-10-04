from __future__ import annotations

from dataclasses import dataclass
from math import exp, log, pi
from statistics import fmean

from .models import MarketRegime, RegimeAssessment, RegimeFeatures


@dataclass(frozen=True, slots=True)
class _ClassStats:
    mean: tuple[float, ...]
    variance: tuple[float, ...]
    prior: float


class GaussianRegimeClassifier:
    """Small deterministic diagonal-Gaussian classifier for regime probabilities.

    This is intentionally transparent and dependency-free. It is a baseline probabilistic
    model, not a claim that Gaussian feature distributions describe markets perfectly.
    """

    def __init__(self, variance_floor: float = 1e-8):
        if variance_floor <= 0:
            raise ValueError("variance_floor must be positive")
        self.variance_floor = variance_floor
        self._stats: dict[MarketRegime, _ClassStats] = {}

    def fit(self, samples: list[tuple[RegimeFeatures, MarketRegime]]) -> "GaussianRegimeClassifier":
        if not samples:
            raise ValueError("samples must not be empty")
        grouped: dict[MarketRegime, list[tuple[float, ...]]] = {}
        for features, label in samples:
            if label == MarketRegime.UNKNOWN:
                continue
            grouped.setdefault(label, []).append(features.as_tuple())
        if len(grouped) < 2:
            raise ValueError("at least two non-UNKNOWN regime classes are required")
        total = sum(len(rows) for rows in grouped.values())
        stats: dict[MarketRegime, _ClassStats] = {}
        for label, rows in grouped.items():
            dims = len(rows[0])
            if any(len(row) != dims for row in rows):
                raise ValueError("inconsistent feature dimensionality")
            means = tuple(fmean(row[j] for row in rows) for j in range(dims))
            variances = tuple(
                max(fmean((row[j] - means[j]) ** 2 for row in rows), self.variance_floor)
                for j in range(dims)
            )
            stats[label] = _ClassStats(means, variances, len(rows) / total)
        self._stats = stats
        return self

    def predict(self, features: RegimeFeatures) -> RegimeAssessment:
        if not self._stats:
            raise RuntimeError("classifier must be fitted before prediction")
        x = features.as_tuple()
        log_scores: dict[MarketRegime, float] = {}
        for label, stats in self._stats.items():
            score = log(stats.prior)
            for value, mean, variance in zip(x, stats.mean, stats.variance, strict=True):
                score += -0.5 * (log(2 * pi * variance) + ((value - mean) ** 2 / variance))
            log_scores[label] = score
        max_log = max(log_scores.values())
        exp_scores = {label: exp(score - max_log) for label, score in log_scores.items()}
        normalizer = sum(exp_scores.values())
        probabilities = {label: value / normalizer for label, value in exp_scores.items()}
        regime = max(probabilities, key=probabilities.get)
        confidence = probabilities[regime]
        ordered = sorted(probabilities.items(), key=lambda kv: kv[1], reverse=True)
        reasons = tuple(f"{label.value}={prob:.6f}" for label, prob in ordered)
        return RegimeAssessment(regime, confidence, probabilities, reasons)
