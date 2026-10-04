from __future__ import annotations

from datetime import datetime
from typing import Iterable

from quant_system.features import FeatureInputBatch
from quant_system.data.infrastructure_gate import DatasetConsumer, InfrastructureDatasetBoundary

from .research_datasets import PartitionDescriptor, PointInTimeDatasetAssembler


class R13FeatureDatasetSource:
    """Adapt an R1.3 PIT research dataset into the Feature Engine input contract.

    The R1.3 manifest fingerprint is the immutable dataset fingerprint carried into
    feature lineage.  The R1.3 universe fingerprint is carried unchanged as the
    Feature Engine ``universe_version``.  R1.3 performs the primary PIT filtering;
    ``FeatureInputBatch`` independently fails closed on any future-available event.
    """

    def __init__(
        self,
        assembler: PointInTimeDatasetAssembler,
        *,
        dataset_id: str,
        partitions: Iterable[PartitionDescriptor],
        start_time: datetime | None = None,
        infrastructure_boundary: InfrastructureDatasetBoundary | None = None,
        consumer: DatasetConsumer | str = DatasetConsumer.FEATURE_ENGINE,
    ) -> None:
        if not dataset_id:
            raise ValueError("dataset_id is required")
        self._assembler = assembler
        self._dataset_id = dataset_id
        self._partitions = tuple(sorted(partitions, key=lambda item: item.relative_path))
        self._start_time = start_time
        self._infrastructure_boundary = infrastructure_boundary
        self._consumer = DatasetConsumer(consumer)
        if infrastructure_boundary is not None:
            infrastructure_boundary.authorize(self._consumer)

    @property
    def infrastructure_boundary_fingerprint(self) -> str | None:
        return None if self._infrastructure_boundary is None else self._infrastructure_boundary.fingerprint

    def load_feature_batch(
        self,
        *,
        instrument_id: str,
        venue: str,
        decision_time: datetime,
    ) -> FeatureInputBatch:
        if self._infrastructure_boundary is not None:
            self._infrastructure_boundary.authorize(self._consumer)
        relevant_partitions = tuple(
            descriptor
            for descriptor in self._partitions
            if descriptor.key.dataset_id == self._dataset_id
            and descriptor.key.instrument_id == instrument_id
            and descriptor.key.venue == venue
        )
        assembled = self._assembler.assemble(
            dataset_id=self._dataset_id,
            partitions=relevant_partitions,
            decision_time=decision_time,
            start_time=self._start_time,
        )
        manifest = assembled.manifest
        return FeatureInputBatch(
            instrument_id=instrument_id,
            venue=venue,
            decision_time=decision_time,
            events=assembled.events,
            dataset_fingerprints=(manifest.fingerprint(),),
            universe_version=manifest.universe_fingerprint,
            infrastructure_boundary_fingerprint=self.infrastructure_boundary_fingerprint,
        )
