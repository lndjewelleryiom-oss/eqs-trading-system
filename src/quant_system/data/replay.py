from __future__ import annotations

from collections.abc import Iterable, Iterator
from datetime import datetime

from quant_system.data.models import MarketObservation
from quant_system.data.quality import DataQualityGate, QualityDisposition


class DeterministicReplay:
    """Stable point-in-time replay ordered by availability, event time and identity."""

    def __init__(self, observations: Iterable[MarketObservation], quality_gate: DataQualityGate | None = None):
        gate = quality_gate or DataQualityGate()
        accepted: list[MarketObservation] = []
        for obs in observations:
            quality = gate.evaluate(obs)
            if quality.disposition == QualityDisposition.ACCEPT:
                accepted.append(obs)
        self._observations = tuple(sorted(
            accepted,
            key=lambda o: (o.available_at, o.event_time, o.dataset_id, o.symbol, o.revision, o.received_at),
        ))

    def iter_until(self, decision_time: datetime) -> Iterator[MarketObservation]:
        if decision_time.tzinfo is None or decision_time.utcoffset() is None:
            raise ValueError("decision_time must be timezone-aware")
        for obs in self._observations:
            if obs.available_at > decision_time:
                break
            obs.assert_usable_at(decision_time)
            yield obs
