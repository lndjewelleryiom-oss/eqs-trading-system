from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Mapping


class DataHealth(StrEnum):
    HEALTHY = "HEALTHY"
    MISSING = "MISSING"
    STALE = "STALE"
    GAP = "GAP"
    CLOCK_SKEW = "CLOCK_SKEW"
    SCHEMA_CHANGE = "SCHEMA_CHANGE"
    CORRECTION_PENDING = "CORRECTION_PENDING"
    QUARANTINED = "QUARANTINED"


BLOCKING_STATES = frozenset({
    DataHealth.MISSING,
    DataHealth.STALE,
    DataHealth.GAP,
    DataHealth.CLOCK_SKEW,
    DataHealth.SCHEMA_CHANGE,
    DataHealth.CORRECTION_PENDING,
    DataHealth.QUARANTINED,
})


@dataclass(frozen=True, slots=True)
class SeriesRequirement:
    series_id: str
    max_age: timedelta
    maximum_clock_skew: timedelta = timedelta(seconds=5)

    def __post_init__(self) -> None:
        if self.max_age <= timedelta(0):
            raise ValueError("max_age must be positive")
        if self.maximum_clock_skew < timedelta(0):
            raise ValueError("maximum_clock_skew must be non-negative")


@dataclass(frozen=True, slots=True)
class SeriesObservation:
    series_id: str
    observed_at: datetime | None
    generation: int
    gap_detected: bool = False
    schema_compatible: bool = True
    correction_pending: bool = False
    quarantined: bool = False

    def __post_init__(self) -> None:
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None
        ):
            raise ValueError("observed_at must be timezone-aware")
        if self.generation < 0:
            raise ValueError("generation must be >= 0")


@dataclass(frozen=True, slots=True)
class SeriesAssessment:
    series_id: str
    state: DataHealth
    generation: int
    age_seconds: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class ScopeAssessment:
    scope_id: str
    state: str
    series: tuple[SeriesAssessment, ...]
    blockers: tuple[str, ...]
    required_revalidation_generation: int
    can_resume: bool


class ScopedDataFailurePolicy:
    """Fail-closed, scope-local data health policy.

    A bad series blocks only scopes that explicitly depend on it. Recovery requires
    a strictly newer, healthy generation than the generation that caused the block.
    """

    def __init__(self, requirements: Mapping[str, tuple[SeriesRequirement, ...]]):
        if not requirements:
            raise ValueError("at least one scope is required")
        self.requirements = {
            scope: tuple(reqs) for scope, reqs in requirements.items()
        }
        self._blocked_generation: dict[str, int] = {}

    @staticmethod
    def assess_series(
        requirement: SeriesRequirement,
        observation: SeriesObservation | None,
        *,
        now: datetime,
    ) -> SeriesAssessment:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if observation is None or observation.observed_at is None:
            return SeriesAssessment(
                requirement.series_id, DataHealth.MISSING, 0, None, "MISSING_SERIES"
            )
        if observation.series_id != requirement.series_id:
            raise ValueError("series identity mismatch")

        observed_at = observation.observed_at
        delta = now - observed_at
        age_seconds = delta.total_seconds()

        if observation.quarantined:
            state, reason = DataHealth.QUARANTINED, "QUARANTINED_SOURCE"
        elif not observation.schema_compatible:
            state, reason = DataHealth.SCHEMA_CHANGE, "SCHEMA_CHANGE_UNVALIDATED"
        elif observation.correction_pending:
            state, reason = DataHealth.CORRECTION_PENDING, "CORRECTION_PENDING"
        elif observation.gap_detected:
            state, reason = DataHealth.GAP, "COVERAGE_GAP"
        elif delta < -requirement.maximum_clock_skew:
            state, reason = DataHealth.CLOCK_SKEW, "FUTURE_OBSERVATION_CLOCK_SKEW"
        elif delta > requirement.max_age:
            state, reason = DataHealth.STALE, "STALE_SERIES"
        else:
            state, reason = DataHealth.HEALTHY, "HEALTHY"
        return SeriesAssessment(
            requirement.series_id,
            state,
            observation.generation,
            age_seconds,
            reason,
        )

    def assess_scope(
        self,
        scope_id: str,
        observations: Mapping[str, SeriesObservation],
        *,
        now: datetime,
    ) -> ScopeAssessment:
        if scope_id not in self.requirements:
            raise KeyError(scope_id)
        rows = tuple(
            self.assess_series(req, observations.get(req.series_id), now=now)
            for req in self.requirements[scope_id]
        )
        blockers = tuple(
            f"{row.series_id}:{row.reason}"
            for row in rows
            if row.state in BLOCKING_STATES
        )
        current_max_generation = max((row.generation for row in rows), default=0)

        if blockers:
            previous = self._blocked_generation.get(scope_id, -1)
            self._blocked_generation[scope_id] = max(previous, current_max_generation)
            required_generation = self._blocked_generation[scope_id] + 1
            return ScopeAssessment(
                scope_id=scope_id,
                state="BLOCKED",
                series=rows,
                blockers=blockers,
                required_revalidation_generation=required_generation,
                can_resume=False,
            )

        blocked_generation = self._blocked_generation.get(scope_id)
        if blocked_generation is not None:
            required_generation = blocked_generation + 1
            if not rows or any(row.generation < required_generation for row in rows):
                return ScopeAssessment(
                    scope_id=scope_id,
                    state="REVALIDATION_REQUIRED",
                    series=rows,
                    blockers=("FRESH_HEALTHY_GENERATION_REQUIRED",),
                    required_revalidation_generation=required_generation,
                    can_resume=False,
                )
            del self._blocked_generation[scope_id]
        else:
            required_generation = current_max_generation

        return ScopeAssessment(
            scope_id=scope_id,
            state="PASS",
            series=rows,
            blockers=(),
            required_revalidation_generation=required_generation,
            can_resume=True,
        )

    def blocked_scopes(self) -> tuple[str, ...]:
        return tuple(sorted(self._blocked_generation))
