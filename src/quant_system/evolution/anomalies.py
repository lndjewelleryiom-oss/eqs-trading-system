from __future__ import annotations

from dataclasses import dataclass
from statistics import fmean, pstdev


@dataclass(frozen=True, slots=True)
class Anomaly:
    kind: str
    score: float
    direction: int
    context: str


class ZScoreAnomalyDetector:
    def __init__(self, threshold: float = 3.0, min_history: int = 20):
        if threshold <= 0:
            raise ValueError("threshold must be positive")
        if min_history < 3:
            raise ValueError("min_history must be >= 3")
        self.threshold = threshold
        self.min_history = min_history

    def detect(self, values: list[float], *, context: str) -> Anomaly | None:
        if len(values) < self.min_history + 1:
            return None
        history = values[:-1]
        sigma = pstdev(history)
        if sigma <= 0:
            return None
        z = (values[-1] - fmean(history)) / sigma
        if abs(z) < self.threshold:
            return None
        return Anomaly("EXTREME_ZSCORE", abs(z), 1 if z > 0 else -1, context)


class CrossSectionalDispersionDetector:
    def __init__(self, threshold: float = 2.5):
        if threshold <= 0:
            raise ValueError("threshold must be positive")
        self.threshold = threshold

    def detect(self, observations: dict[str, float]) -> tuple[Anomaly, ...]:
        if len(observations) < 3:
            return ()
        values = list(observations.values())
        mean = fmean(values)
        sigma = pstdev(values)
        if sigma <= 0:
            return ()
        anomalies = []
        for symbol, value in sorted(observations.items()):
            z = (value - mean) / sigma
            if abs(z) >= self.threshold:
                anomalies.append(Anomaly("CROSS_SECTION_OUTLIER", abs(z), 1 if z > 0 else -1, symbol))
        return tuple(anomalies)
