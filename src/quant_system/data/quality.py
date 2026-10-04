from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from quant_system.data.models import MarketObservation


class QualityDisposition(StrEnum):
    ACCEPT = "ACCEPT"
    QUARANTINE = "QUARANTINE"
    REJECT = "REJECT"


@dataclass(frozen=True, slots=True)
class QualityResult:
    disposition: QualityDisposition
    reason_codes: tuple[str, ...]


class DataQualityGate:
    def evaluate(self, obs: MarketObservation) -> QualityResult:
        reasons: list[str] = []
        if obs.available_at < obs.published_at:
            reasons.append("AVAILABLE_BEFORE_PUBLISHED")
        if obs.received_at < obs.available_at:
            reasons.append("RECEIVED_BEFORE_AVAILABLE")
        if not isfinite(obs.value):
            reasons.append("NON_FINITE_VALUE")
        if obs.suspected_corruption:
            reasons.append("SUSPECTED_CORRUPTION")
        if obs.confidence < 0.5:
            reasons.append("LOW_CONFIDENCE")

        hard = {"AVAILABLE_BEFORE_PUBLISHED", "RECEIVED_BEFORE_AVAILABLE", "NON_FINITE_VALUE"}
        if hard.intersection(reasons):
            return QualityResult(QualityDisposition.REJECT, tuple(reasons))
        if reasons:
            return QualityResult(QualityDisposition.QUARANTINE, tuple(reasons))
        return QualityResult(QualityDisposition.ACCEPT, ("QUALITY_OK",))


@dataclass(frozen=True, slots=True)
class QuarantineRecord:
    observation: MarketObservation
    disposition: QualityDisposition
    reason_codes: tuple[str, ...]


class RevisionQualityPipeline:
    """Stateful quality job that quarantines suspicious data and enforces revision integrity."""

    def __init__(self, gate: DataQualityGate | None = None):
        self.gate = gate or DataQualityGate()
        self.accepted: list[MarketObservation] = []
        self.quarantine: list[QuarantineRecord] = []
        self.rejected: list[QuarantineRecord] = []
        self._revisions: dict[tuple[str, str, object], dict[int, MarketObservation]] = {}

    def process(self, obs: MarketObservation) -> QualityResult:
        result = self.gate.evaluate(obs)
        if result.disposition == QualityDisposition.ACCEPT:
            key = (obs.dataset_id, obs.symbol, obs.event_time)
            revisions = self._revisions.setdefault(key, {})
            existing = revisions.get(obs.revision)
            if existing is not None and existing != obs:
                result = QualityResult(QualityDisposition.REJECT, ("CONFLICTING_SAME_REVISION",))
            elif revisions and obs.revision < max(revisions):
                result = QualityResult(QualityDisposition.QUARANTINE, ("OUT_OF_ORDER_REVISION",))
            else:
                revisions[obs.revision] = obs

        if result.disposition == QualityDisposition.ACCEPT:
            self.accepted.append(obs)
        else:
            record = QuarantineRecord(obs, result.disposition, result.reason_codes)
            if result.disposition == QualityDisposition.QUARANTINE:
                self.quarantine.append(record)
            else:
                self.rejected.append(record)
        return result
