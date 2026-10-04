from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from math import log


class DegradationState(StrEnum):
    ACTIVE = "ACTIVE"
    WATCH = "WATCH"
    REDUCED = "REDUCED"
    PAUSED = "PAUSED"
    RESEARCH = "RESEARCH"
    RETIRED = "RETIRED"


class Severity(IntEnum):
    OK = 0
    WATCH = 1
    REDUCE = 2
    PAUSE = 3


@dataclass(frozen=True, slots=True)
class ExpectedBehavior:
    win_rate: float
    expectancy: float
    sharpe: float
    max_drawdown: float
    slippage_bps: float
    trades_per_period: float


@dataclass(frozen=True, slots=True)
class ObservedBehavior:
    win_rate: float
    expectancy: float
    sharpe: float
    drawdown: float
    slippage_bps: float
    trades_per_period: float


@dataclass(frozen=True, slots=True)
class DriftThresholds:
    watch_relative_deviation: float = 0.20
    reduce_relative_deviation: float = 0.35
    pause_relative_deviation: float = 0.60
    pause_drawdown_multiplier: float = 1.25
    population_stability_watch: float = 0.10
    population_stability_reduce: float = 0.20
    population_stability_pause: float = 0.35


@dataclass(frozen=True, slots=True)
class DegradationAssessment:
    state: DegradationState
    allocation_multiplier: float
    severity: Severity
    reasons: tuple[str, ...]
    metrics: dict[str, float]


def population_stability_index(expected: list[float], observed: list[float], *, bins: int = 10) -> float:
    if len(expected) < bins or len(observed) < bins:
        raise ValueError("both samples must contain at least bins observations")
    if bins < 2:
        raise ValueError("bins must be >= 2")
    sorted_expected = sorted(expected)
    edges = [sorted_expected[min(len(sorted_expected) - 1, int(len(sorted_expected) * i / bins))] for i in range(1, bins)]

    def counts(values: list[float]) -> list[int]:
        result = [0] * bins
        for value in values:
            index = 0
            while index < len(edges) and value > edges[index]:
                index += 1
            result[index] += 1
        return result

    expected_counts = counts(expected)
    observed_counts = counts(observed)
    eps = 1e-9
    psi = 0.0
    for exp_count, obs_count in zip(expected_counts, observed_counts, strict=True):
        exp_fraction = max(exp_count / len(expected), eps)
        obs_fraction = max(obs_count / len(observed), eps)
        psi += (obs_fraction - exp_fraction) * log(obs_fraction / exp_fraction)
    return psi


class DegradationMonitor:
    def __init__(self, thresholds: DriftThresholds = DriftThresholds()):
        self.thresholds = thresholds

    def assess(
        self,
        expected: ExpectedBehavior,
        observed: ObservedBehavior,
        *,
        feature_psi: float = 0.0,
        prediction_psi: float = 0.0,
        execution_error: bool = False,
        data_integrity_error: bool = False,
    ) -> DegradationAssessment:
        if feature_psi < 0 or prediction_psi < 0:
            raise ValueError("PSI values must be non-negative")
        if data_integrity_error or execution_error:
            reason = "DATA_INTEGRITY_ERROR" if data_integrity_error else "EXECUTION_ERROR"
            return DegradationAssessment(
                DegradationState.PAUSED, 0.0, Severity.PAUSE, (reason,), {"feature_psi": feature_psi, "prediction_psi": prediction_psi}
            )

        metric_deviations = {
            "win_rate": self._downside_deviation(expected.win_rate, observed.win_rate),
            "expectancy": self._downside_deviation(expected.expectancy, observed.expectancy),
            "sharpe": self._downside_deviation(expected.sharpe, observed.sharpe),
            "slippage": self._upside_deviation(expected.slippage_bps, observed.slippage_bps),
            "trade_frequency": self._two_sided_deviation(expected.trades_per_period, observed.trades_per_period),
        }
        reasons: list[str] = []
        severity = Severity.OK
        for name, deviation in metric_deviations.items():
            metric_severity = self._severity_for_relative_deviation(deviation)
            if metric_severity > Severity.OK:
                reasons.append(f"{name.upper()}_DEVIATION={deviation:.4f}")
            severity = max(severity, metric_severity)

        if expected.max_drawdown > 0:
            dd_ratio = observed.drawdown / expected.max_drawdown
        else:
            dd_ratio = float("inf") if observed.drawdown > 0 else 0.0
        if dd_ratio >= self.thresholds.pause_drawdown_multiplier:
            severity = Severity.PAUSE
            reasons.append(f"DRAWDOWN_LIMIT_RATIO={dd_ratio:.4f}")

        max_psi = max(feature_psi, prediction_psi)
        if max_psi >= self.thresholds.population_stability_pause:
            severity = max(severity, Severity.PAUSE)
            reasons.append(f"SEVERE_DISTRIBUTION_DRIFT={max_psi:.4f}")
        elif max_psi >= self.thresholds.population_stability_reduce:
            severity = max(severity, Severity.REDUCE)
            reasons.append(f"DISTRIBUTION_DRIFT={max_psi:.4f}")
        elif max_psi >= self.thresholds.population_stability_watch:
            severity = max(severity, Severity.WATCH)
            reasons.append(f"EARLY_DISTRIBUTION_DRIFT={max_psi:.4f}")

        state, multiplier = {
            Severity.OK: (DegradationState.ACTIVE, 1.0),
            Severity.WATCH: (DegradationState.WATCH, 0.75),
            Severity.REDUCE: (DegradationState.REDUCED, 0.40),
            Severity.PAUSE: (DegradationState.PAUSED, 0.0),
        }[severity]
        if not reasons:
            reasons.append("WITHIN_EXPECTED_BEHAVIOR")
        metrics = dict(metric_deviations)
        metrics.update({"drawdown_ratio": dd_ratio, "feature_psi": feature_psi, "prediction_psi": prediction_psi})
        return DegradationAssessment(state, multiplier, severity, tuple(reasons), metrics)

    def _severity_for_relative_deviation(self, deviation: float) -> Severity:
        if deviation >= self.thresholds.pause_relative_deviation:
            return Severity.PAUSE
        if deviation >= self.thresholds.reduce_relative_deviation:
            return Severity.REDUCE
        if deviation >= self.thresholds.watch_relative_deviation:
            return Severity.WATCH
        return Severity.OK

    @staticmethod
    def _downside_deviation(expected: float, observed: float) -> float:
        scale = max(abs(expected), 1e-12)
        return max(0.0, (expected - observed) / scale)

    @staticmethod
    def _upside_deviation(expected: float, observed: float) -> float:
        scale = max(abs(expected), 1e-12)
        return max(0.0, (observed - expected) / scale)

    @staticmethod
    def _two_sided_deviation(expected: float, observed: float) -> float:
        scale = max(abs(expected), 1e-12)
        return abs(observed - expected) / scale
