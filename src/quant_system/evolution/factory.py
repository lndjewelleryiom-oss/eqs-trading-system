from __future__ import annotations

from dataclasses import dataclass

from .anomalies import Anomaly
from .models import AcceptanceCriteria, Hypothesis


@dataclass(frozen=True, slots=True)
class StrategyBlueprint:
    hypothesis_id: str
    family: str
    asset_class: str
    horizon: str
    signal_description: str
    falsification_tests: tuple[str, ...]


class HypothesisFactory:
    """Converts detected anomalies into falsifiable hypotheses, not parameter searches."""

    def from_anomaly(
        self,
        anomaly: Anomaly,
        *,
        asset_class: str,
        horizon: str,
        criteria: AcceptanceCriteria = AcceptanceCriteria(),
    ) -> Hypothesis:
        if anomaly.kind == "EXTREME_ZSCORE":
            direction = "positive" if anomaly.direction > 0 else "negative"
            statement = (
                f"After an extreme {direction} standardized move in {anomaly.context}, "
                "subsequent returns differ from their unconditional distribution after costs."
            )
            rationale = "Extreme moves may reflect forced flow, information arrival, or temporary liquidity imbalance; direction must be tested."
            family = "event-response"
        elif anomaly.kind == "CROSS_SECTION_OUTLIER":
            statement = (
                f"Cross-sectional outlier behavior in {anomaly.context} contains incremental information for relative-value returns after costs."
            )
            rationale = "Unusual relative moves may reflect delayed common-factor adjustment or idiosyncratic information; both continuation and reversal must be falsified."
            family = "cross-sectional-relative-value"
        else:
            raise ValueError(f"unsupported anomaly kind: {anomaly.kind}")
        return Hypothesis(statement, rationale, family, asset_class, horizon, f"{anomaly.kind}:{anomaly.context}", criteria)

    def blueprint(self, hypothesis: Hypothesis) -> StrategyBlueprint:
        return StrategyBlueprint(
            str(hypothesis.hypothesis_id),
            hypothesis.family,
            hypothesis.asset_class,
            hypothesis.horizon,
            hypothesis.statement,
            (
                "chronological out-of-sample after-cost test",
                "walk-forward stability",
                "parameter perturbation surface",
                "cost/slippage stress",
                "multiple-testing/selection-bias correction",
                "regime and crisis slicing",
            ),
        )
