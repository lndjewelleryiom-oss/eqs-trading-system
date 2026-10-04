from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from quant_system.risk.contracts import CapitalMandate

class MandateStatus(StrEnum):
    ACTIVE="ACTIVE"
    SUPERSEDED="SUPERSEDED"
    REVOKED="REVOKED"
    EXPIRED="EXPIRED"
    REVIEW_DUE="REVIEW_DUE"

@dataclass(frozen=True, slots=True)
class MandateRecord:
    mandate: CapitalMandate
    status: MandateStatus
    reason: str

class MandateRegistry:
    """Version/revocation semantics only; EQS-08 owns lifecycle decisions."""

    def __init__(self):
        self._records: dict[str, MandateRecord] = {}

    def issue(self, mandate: CapitalMandate) -> MandateRecord:
        current = self._records.get(mandate.mandate_id)
        if current is not None and mandate.version <= current.mandate.version:
            raise ValueError("mandate version must increase")
        if current is not None:
            self._records[mandate.mandate_id] = MandateRecord(
                current.mandate, MandateStatus.SUPERSEDED, "NEW_VERSION")
        record = MandateRecord(mandate, MandateStatus.ACTIVE, "ISSUED")
        self._records[mandate.mandate_id] = record
        return record
    def revoke(self, mandate_id: str, reason: str) -> MandateRecord:
        current = self._records[mandate_id]
        record = MandateRecord(current.mandate, MandateStatus.REVOKED, reason)
        self._records[mandate_id] = record
        return record

    def status(self, mandate_id: str, *, now: datetime) -> MandateRecord:
        current = self._records[mandate_id]
        if current.status in {MandateStatus.REVOKED, MandateStatus.SUPERSEDED}:
            return current
        if now >= current.mandate.valid_until:
            return MandateRecord(current.mandate, MandateStatus.EXPIRED, "VALIDITY_ENDED")
        if now >= current.mandate.review_at:
            return MandateRecord(current.mandate, MandateStatus.REVIEW_DUE, "REVIEW_REQUIRED")
        return current

    @staticmethod
    def reservation_action(status: MandateStatus) -> str:
        # Existing execution ambiguity must not release capacity.
        if status in {MandateStatus.REVOKED, MandateStatus.SUPERSEDED,
                      MandateStatus.EXPIRED, MandateStatus.REVIEW_DUE}:
            return "BLOCK_NEW_KEEP_EXISTING_RESERVATIONS"
        return "UNCHANGED"
