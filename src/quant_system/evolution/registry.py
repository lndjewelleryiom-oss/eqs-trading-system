from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from .models import Hypothesis, HypothesisRecord, HypothesisStatus, ResearchEvidence


class HypothesisRegistry:
    def __init__(self):
        self._records: dict[UUID, HypothesisRecord] = {}

    def register(self, hypothesis: Hypothesis) -> HypothesisRecord:
        if hypothesis.hypothesis_id in self._records:
            raise ValueError("hypothesis already registered")
        record = HypothesisRecord(hypothesis, HypothesisStatus.PROPOSED, hypothesis.criteria.fingerprint)
        self._records[hypothesis.hypothesis_id] = record
        return record

    def transition(self, hypothesis_id: UUID, status: HypothesisStatus, *, reasons: tuple[str, ...] = (), evidence: ResearchEvidence | None = None) -> HypothesisRecord:
        record = self.get(hypothesis_id)
        if record.hypothesis.criteria.fingerprint != record.criteria_fingerprint:
            raise ValueError("acceptance criteria changed after registration")
        allowed = {
            HypothesisStatus.PROPOSED: {HypothesisStatus.QUEUED, HypothesisStatus.REJECTED},
            HypothesisStatus.QUEUED: {HypothesisStatus.TESTING, HypothesisStatus.REJECTED},
            HypothesisStatus.TESTING: {HypothesisStatus.CANDIDATE, HypothesisStatus.REJECTED},
            HypothesisStatus.CANDIDATE: {HypothesisStatus.RETIRED},
            HypothesisStatus.REJECTED: {HypothesisStatus.QUEUED, HypothesisStatus.RETIRED},
            HypothesisStatus.RETIRED: set(),
        }[record.status]
        if status not in allowed:
            raise ValueError(f"illegal hypothesis transition {record.status}->{status}")
        updated = replace(record, status=status, reasons=tuple(reasons), evidence=evidence)
        self._records[hypothesis_id] = updated
        return updated

    def get(self, hypothesis_id: UUID) -> HypothesisRecord:
        try:
            return self._records[hypothesis_id]
        except KeyError as exc:
            raise KeyError(f"unknown hypothesis {hypothesis_id}") from exc

    def records(self) -> tuple[HypothesisRecord, ...]:
        return tuple(self._records[key] for key in sorted(self._records, key=str))
