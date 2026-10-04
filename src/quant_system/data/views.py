from collections import defaultdict
from datetime import datetime
from typing import Iterable

from quant_system.data.models import MarketObservation
from quant_system.data.quality import DataQualityGate, QualityDisposition


def latest_point_in_time_view(
    observations: Iterable[MarketObservation],
    decision_time: datetime,
    quality_gate: DataQualityGate | None = None,
) -> tuple[MarketObservation, ...]:
    """Return the latest known revision for each dataset/symbol/event_time key.

    A later revision is visible only after its own availability timestamp.
    """
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise ValueError("decision_time must be timezone-aware")
    gate = quality_gate or DataQualityGate()
    grouped: dict[tuple[str, str, datetime], list[MarketObservation]] = defaultdict(list)
    for obs in observations:
        if obs.available_at > decision_time:
            continue
        if gate.evaluate(obs).disposition != QualityDisposition.ACCEPT:
            continue
        grouped[(obs.dataset_id, obs.symbol, obs.event_time)].append(obs)

    chosen = []
    for items in grouped.values():
        latest = max(items, key=lambda o: (o.revision, o.available_at, o.received_at))
        latest.assert_usable_at(decision_time)
        chosen.append(latest)
    return tuple(sorted(chosen, key=lambda o: (o.event_time, o.dataset_id, o.symbol)))
