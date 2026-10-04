from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping, TypeAlias

from quant_system.data.crypto_perps.models import (
    BookUpdate,
    LiquidationEvent,
    PerpetualStateEvent,
    TradeEvent,
)


MarketEvent: TypeAlias = TradeEvent | BookUpdate | PerpetualStateEvent | LiquidationEvent
FeatureScalar: TypeAlias = float | str
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FeatureFamily(StrEnum):
    MICROSTRUCTURE = "microstructure"
    FUNDING = "funding"
    BASIS = "basis"
    OPEN_INTEREST = "open_interest"
    LIQUIDATION = "liquidation"
    VOLATILITY = "volatility"
    MOMENTUM = "momentum"
    REGIME = "regime"


class FeatureLeakageError(ValueError):
    """Raised when a feature request contains information unavailable at decision time."""


class FeatureInputError(ValueError):
    """Raised when a feature input batch violates canonical identity or lineage rules."""


def _assert_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _normalise(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("feature manifest values must be finite")
        # Stable textual round-trip precision, independent of repr formatting changes.
        return float(format(value, ".17g"))
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    raise TypeError(f"unsupported canonical feature value: {type(value).__name__}")


def _fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        _normalise(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SourceEventRef:
    event_id: str
    available_at: datetime
    raw_sha256: str

    def __post_init__(self) -> None:
        _assert_aware(self.available_at, "available_at")
        if not _SHA256_RE.fullmatch(self.event_id):
            raise ValueError("event_id must be a SHA-256 hex digest")
        if not _SHA256_RE.fullmatch(self.raw_sha256):
            raise ValueError("raw_sha256 must be a SHA-256 hex digest")

    def canonical_payload(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "available_at": self.available_at.isoformat(),
            "raw_sha256": self.raw_sha256,
        }


@dataclass(frozen=True, slots=True)
class FeatureDefinition:
    name: str
    family: FeatureFamily
    version: str
    unit: str
    description: str
    parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.version.strip() or not self.unit.strip():
            raise ValueError("feature name, version and unit are required")
        if not self.description.strip():
            raise ValueError("feature description is required")
        if tuple(sorted(self.parameters)) != self.parameters:
            raise ValueError("feature parameters must be sorted for canonical representation")
        if len({key for key, _ in self.parameters}) != len(self.parameters):
            raise ValueError("feature parameter keys must be unique")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "family": self.family.value,
            "version": self.version,
            "unit": self.unit,
            "description": self.description,
            "parameters": list(self.parameters),
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.canonical_payload())

    @property
    def feature_id(self) -> str:
        return f"{self.name}@{self.version}:{self.fingerprint[:12]}"


@dataclass(frozen=True, slots=True)
class FeatureRecord:
    definition: FeatureDefinition
    instrument_id: str
    venue: str
    decision_time: datetime
    value: FeatureScalar
    source_events: tuple[SourceEventRef, ...]
    input_dataset_fingerprints: tuple[str, ...]
    universe_version: str
    engine_version: str
    config_fingerprint: str

    def __post_init__(self) -> None:
        _assert_aware(self.decision_time, "decision_time")
        if not self.instrument_id or not self.venue:
            raise ValueError("instrument_id and venue are required")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("feature values must be finite")
        if not self.source_events:
            raise FeatureInputError("feature record must retain source-event lineage")
        if any(ref.available_at > self.decision_time for ref in self.source_events):
            raise FeatureLeakageError("feature source was unavailable at decision time")
        if tuple(sorted(self.source_events, key=lambda ref: (ref.available_at, ref.event_id))) != self.source_events:
            raise ValueError("source_events must be canonically sorted")
        if not self.input_dataset_fingerprints:
            raise FeatureInputError("input dataset fingerprints are required")
        if tuple(sorted(self.input_dataset_fingerprints)) != self.input_dataset_fingerprints:
            raise ValueError("input dataset fingerprints must be sorted")
        if any(not _SHA256_RE.fullmatch(value) for value in self.input_dataset_fingerprints):
            raise ValueError("input dataset fingerprints must be SHA-256 hex digests")
        if not self.universe_version or not self.engine_version:
            raise ValueError("universe_version and engine_version are required")
        if not _SHA256_RE.fullmatch(self.config_fingerprint):
            raise ValueError("config_fingerprint must be a SHA-256 hex digest")

    @property
    def source_max_available_at(self) -> datetime:
        return max(ref.available_at for ref in self.source_events)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "feature_definition_fingerprint": self.definition.fingerprint,
            "feature_id": self.definition.feature_id,
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "decision_time": self.decision_time.isoformat(),
            "value": self.value,
            "source_events": [ref.canonical_payload() for ref in self.source_events],
            "source_max_available_at": self.source_max_available_at.isoformat(),
            "input_dataset_fingerprints": self.input_dataset_fingerprints,
            "universe_version": self.universe_version,
            "engine_version": self.engine_version,
            "config_fingerprint": self.config_fingerprint,
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class FeatureInputBatch:
    """Dataset-interface seam used by the Feature Engine.

    R1.3 may later construct this batch from partitioned canonical datasets. The feature
    layer intentionally depends only on canonical R1.1 market-event contracts plus
    immutable dataset/universe fingerprints.
    """

    instrument_id: str
    venue: str
    decision_time: datetime
    events: tuple[MarketEvent, ...]
    dataset_fingerprints: tuple[str, ...]
    universe_version: str
    infrastructure_boundary_fingerprint: str | None = None

    def __post_init__(self) -> None:
        _assert_aware(self.decision_time, "decision_time")
        if not self.instrument_id or not self.venue or not self.universe_version:
            raise FeatureInputError("instrument_id, venue and universe_version are required")
        if not self.events:
            raise FeatureInputError("feature batch cannot be empty")
        if not self.dataset_fingerprints:
            raise FeatureInputError("dataset fingerprints are required")
        canonical_fingerprints = tuple(sorted(set(self.dataset_fingerprints)))
        if canonical_fingerprints != self.dataset_fingerprints:
            raise FeatureInputError("dataset fingerprints must be unique and canonically sorted")
        if any(not _SHA256_RE.fullmatch(value) for value in self.dataset_fingerprints):
            raise FeatureInputError("dataset fingerprints must be SHA-256 hex digests")
        if self.infrastructure_boundary_fingerprint is not None and not _SHA256_RE.fullmatch(self.infrastructure_boundary_fingerprint):
            raise FeatureInputError("infrastructure boundary fingerprint must be SHA-256")
        for event in self.events:
            meta = event.meta
            if meta.instrument_id != self.instrument_id or meta.venue != self.venue:
                raise FeatureInputError("all events must match batch instrument_id and venue")
            if meta.available_at > self.decision_time:
                raise FeatureLeakageError(
                    f"event {meta.canonical_identity()} was unavailable at decision time"
                )

    @property
    def source_event_refs(self) -> tuple[SourceEventRef, ...]:
        refs = {
            (event.meta.canonical_identity(), event.meta.available_at, event.meta.raw_sha256): SourceEventRef(
                event_id=event.meta.canonical_identity(),
                available_at=event.meta.available_at,
                raw_sha256=event.meta.raw_sha256,
            )
            for event in self.events
        }
        return tuple(sorted(refs.values(), key=lambda ref: (ref.available_at, ref.event_id)))

    @property
    def fingerprint(self) -> str:
        return _fingerprint({
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "decision_time": self.decision_time.isoformat(),
            "dataset_fingerprints": self.dataset_fingerprints,
            "universe_version": self.universe_version,
            "infrastructure_boundary_fingerprint": self.infrastructure_boundary_fingerprint,
            "source_events": [ref.canonical_payload() for ref in self.source_event_refs],
        })


@dataclass(frozen=True, slots=True)
class FeatureRunManifest:
    engine_version: str
    config_fingerprint: str
    registry_fingerprint: str
    input_batch_fingerprint: str
    instrument_id: str
    venue: str
    decision_time: datetime
    input_dataset_fingerprints: tuple[str, ...]
    universe_version: str
    output_fingerprints: tuple[str, ...]

    def __post_init__(self) -> None:
        _assert_aware(self.decision_time, "decision_time")
        for value in (self.config_fingerprint, self.registry_fingerprint, self.input_batch_fingerprint):
            if not _SHA256_RE.fullmatch(value):
                raise ValueError("manifest fingerprints must be SHA-256 hex digests")
        if tuple(sorted(self.output_fingerprints)) != self.output_fingerprints:
            raise ValueError("output_fingerprints must be sorted")
        if any(not _SHA256_RE.fullmatch(value) for value in self.output_fingerprints):
            raise ValueError("output fingerprints must be SHA-256 hex digests")

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "engine_version": self.engine_version,
            "config_fingerprint": self.config_fingerprint,
            "registry_fingerprint": self.registry_fingerprint,
            "input_batch_fingerprint": self.input_batch_fingerprint,
            "instrument_id": self.instrument_id,
            "venue": self.venue,
            "decision_time": self.decision_time.isoformat(),
            "input_dataset_fingerprints": self.input_dataset_fingerprints,
            "universe_version": self.universe_version,
            "output_fingerprints": self.output_fingerprints,
        }

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.canonical_payload())
