from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Iterable

from .calculators import (
    BasisCalculator,
    ComputedFeature,
    FeatureCalculator,
    FeatureContext,
    FundingCalculator,
    LiquidationCalculator,
    MicrostructureCalculator,
    MomentumCalculator,
    OpenInterestCalculator,
    RegimeStateCalculator,
    VolatilityCalculator,
)
from .config import FeatureEngineConfig
from .models import (
    FeatureDefinition,
    FeatureInputBatch,
    FeatureInputError,
    FeatureRecord,
    FeatureRunManifest,
    SourceEventRef,
)


DEFAULT_CALCULATORS: tuple[FeatureCalculator, ...] = (
    MicrostructureCalculator(),
    FundingCalculator(),
    BasisCalculator(),
    OpenInterestCalculator(),
    LiquidationCalculator(),
    VolatilityCalculator(),
    MomentumCalculator(),
    RegimeStateCalculator(),
)


@dataclass(frozen=True, slots=True)
class FeatureRegistry:
    definitions: tuple[FeatureDefinition, ...]

    @classmethod
    def from_calculators(
        cls, calculators: Iterable[FeatureCalculator], config: FeatureEngineConfig
    ) -> "FeatureRegistry":
        definitions = tuple(
            sorted(
                (definition for calculator in calculators for definition in calculator.definitions(config)),
                key=lambda definition: (definition.name, definition.version, definition.fingerprint),
            )
        )
        keys = [(definition.name, definition.version) for definition in definitions]
        if len(set(keys)) != len(keys):
            raise FeatureInputError("feature registry contains duplicate name/version definitions")
        return cls(definitions=definitions)

    @property
    def fingerprint(self) -> str:
        payload = [definition.canonical_payload() for definition in self.definitions]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureRun:
    records: tuple[FeatureRecord, ...]
    manifest: FeatureRunManifest

    def by_name(self) -> dict[str, FeatureRecord]:
        return {record.definition.name: record for record in self.records}


class CryptoPerpetualFeatureEngine:
    """Deterministic point-in-time feature engine over canonical crypto-perp events.

    This layer performs feature construction only. It contains no strategy ranking,
    alpha selection, portfolio allocation, order generation or broker submission path.
    """

    def __init__(
        self,
        config: FeatureEngineConfig = FeatureEngineConfig(),
        calculators: tuple[FeatureCalculator, ...] = DEFAULT_CALCULATORS,
    ) -> None:
        self.config = config
        self.calculators = calculators
        self.registry = FeatureRegistry.from_calculators(calculators, config)

    @staticmethod
    def _event_key(event: object) -> tuple[object, ...]:
        meta = event.meta  # type: ignore[attr-defined]
        return (meta.event_time, meta.published_at, meta.available_at, meta.canonical_identity())

    @staticmethod
    def _source_refs(computed: ComputedFeature) -> tuple[SourceEventRef, ...]:
        unique = {
            (event.meta.canonical_identity(), event.meta.available_at, event.meta.raw_sha256): SourceEventRef(
                event_id=event.meta.canonical_identity(),
                available_at=event.meta.available_at,
                raw_sha256=event.meta.raw_sha256,
            )
            for event in computed.source_events
        }
        return tuple(sorted(unique.values(), key=lambda ref: (ref.available_at, ref.event_id)))

    def compute(self, batch: FeatureInputBatch) -> FeatureRun:
        # FeatureInputBatch has already applied fail-closed PIT/identity validation.
        ordered_events = tuple(sorted(batch.events, key=self._event_key))
        prior: dict[str, ComputedFeature] = {}
        context = FeatureContext(
            batch_events=ordered_events,
            decision_time=batch.decision_time,
            config=self.config,
            prior=prior,
        )
        computed: list[ComputedFeature] = []
        for calculator in self.calculators:
            results = calculator.compute(context)
            for result in results:
                key = result.definition.name
                if key in prior:
                    raise FeatureInputError(f"duplicate computed feature name: {key}")
                if not result.source_events:
                    raise FeatureInputError(f"feature {key} omitted source-event lineage")
                prior[key] = result
                computed.append(result)

        records = tuple(sorted((
            FeatureRecord(
                definition=result.definition,
                instrument_id=batch.instrument_id,
                venue=batch.venue,
                decision_time=batch.decision_time,
                value=result.value,
                source_events=self._source_refs(result),
                input_dataset_fingerprints=batch.dataset_fingerprints,
                universe_version=batch.universe_version,
                engine_version=self.config.engine_version,
                config_fingerprint=self.config.fingerprint,
            )
            for result in computed
        ), key=lambda record: (record.definition.name, record.definition.version)))
        output_fingerprints = tuple(sorted(record.fingerprint for record in records))
        manifest = FeatureRunManifest(
            engine_version=self.config.engine_version,
            config_fingerprint=self.config.fingerprint,
            registry_fingerprint=self.registry.fingerprint,
            input_batch_fingerprint=batch.fingerprint,
            instrument_id=batch.instrument_id,
            venue=batch.venue,
            decision_time=batch.decision_time,
            input_dataset_fingerprints=batch.dataset_fingerprints,
            universe_version=batch.universe_version,
            output_fingerprints=output_fingerprints,
        )
        return FeatureRun(records=records, manifest=manifest)
