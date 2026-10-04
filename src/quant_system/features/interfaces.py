from __future__ import annotations

from datetime import datetime
from typing import Protocol

from .models import FeatureInputBatch


class FeatureDatasetSource(Protocol):
    """R1.3 integration seam for loading an as-of canonical feature batch.

    Implementations belong to the dataset layer. They must enforce partition/revision
    rules and return only events whose availability is at or before ``decision_time``;
    FeatureInputBatch performs an independent fail-closed leakage check.
    """

    def load_feature_batch(
        self,
        *,
        instrument_id: str,
        venue: str,
        decision_time: datetime,
    ) -> FeatureInputBatch: ...
