from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from quant_system.core.enums import OrderType, Side
from quant_system.risk.contracts import RiskReservation
from quant_system.risk.interfaces import ExecutionTruth

@dataclass(frozen=True, slots=True)
class CapitalExecutionAuthorityV1:
    reservation_id: UUID
    risk_decision_id: UUID
    intent_id: UUID
    mandate_id: str
    mandate_version: int
    strategy_id: UUID
    strategy_version: str
    portfolio_id: str
    venue: str
    instrument: str
    side: Side
    order_type: OrderType
    requested_quantity: Decimal
    authorised_quantity: Decimal
    reference_price: Decimal
    limit_price: Decimal | None
    reduce_only: bool
    reserved_notional: Decimal
    authority_created_at: datetime
    authority_expires_at: datetime | None
    risk_state_ref: str
    policy_version: str
    execution_mode: str
    schema_version: str = "eqs06-capital-execution-authority-v1"

    def __post_init__(self):
        if self.execution_mode not in {"PAPER","SHADOW"}:
            raise ValueError("LIVE capital authority is disabled")
        if self.authorised_quantity <= 0 or self.authorised_quantity > self.requested_quantity:
            raise ValueError("invalid authorised quantity")
        if self.reserved_notional < self.authorised_quantity * self.reference_price:
            raise ValueError("reservation does not cover authorised notional")

@dataclass(frozen=True, slots=True)
class EQS07ExecutionTruthV2:
    reservation_id: UUID
    execution_id: UUID
    intent_id: UUID
    venue: str
    instrument: str
    execution_state: ExecutionTruth
    cumulative_filled_notional: Decimal
    truth_sequence: int
    reconciliation_generation: int
    observed_at: datetime
    evidence_ref: str
    schema_version: str = "eqs06-eqs07-execution-truth-v2"

    def __post_init__(self):
        if self.truth_sequence < 1 or self.reconciliation_generation < 0:
            raise ValueError("invalid execution truth sequence/generation")
        if self.cumulative_filled_notional < 0:
            raise ValueError("negative cumulative fill")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")

class TruthConflict(RuntimeError):
    pass

class ExecutionTruthConsumer:
    """Monotonic EQS-07 truth gate before reservation settlement."""

    def __init__(self, reservation_store):
        self.store = reservation_store
        self._seen: dict[UUID, EQS07ExecutionTruthV2] = {}

    def apply(self, truth: EQS07ExecutionTruthV2) -> RiskReservation:
        reservation = self.store.get(truth.reservation_id)
        if reservation.intent_id != truth.intent_id:
            raise TruthConflict("INTENT_ID_MISMATCH")
        if reservation.venue != truth.venue or reservation.instrument != truth.instrument:
            raise TruthConflict("VENUE_OR_INSTRUMENT_MISMATCH")
        if truth.cumulative_filled_notional > reservation.reserved_notional:
            raise TruthConflict("FILL_EXCEEDS_RESERVATION")
        prior = self._seen.get(truth.reservation_id)
        cursor = self.store.truth_cursor(truth.reservation_id)
        if prior is None and cursor is not None:
            prior = EQS07ExecutionTruthV2(
                UUID(cursor["reservation_id"]), UUID(cursor["execution_id"]),
                UUID(cursor["intent_id"]), cursor["venue"], cursor["instrument"],
                ExecutionTruth(cursor["execution_state"]),
                Decimal(cursor["cumulative_filled_notional"]), int(cursor["truth_sequence"]),
                int(cursor["reconciliation_generation"]),
                datetime.fromisoformat(cursor["observed_at"]), cursor["evidence_ref"])
        if prior is not None:
            if truth.reconciliation_generation < prior.reconciliation_generation:
                raise TruthConflict("RECONCILIATION_GENERATION_REGRESSION")
            if (truth.reconciliation_generation == prior.reconciliation_generation
                    and truth.truth_sequence < prior.truth_sequence):
                raise TruthConflict("TRUTH_SEQUENCE_REGRESSION")
            if truth.cumulative_filled_notional < prior.cumulative_filled_notional:
                raise TruthConflict("CUMULATIVE_FILL_REGRESSION")
            if truth.truth_sequence == prior.truth_sequence:
                if truth == prior:
                    return reservation
                raise TruthConflict("CONTRADICTORY_DUPLICATE_TRUTH")
            terminal = {ExecutionTruth.FILLED, ExecutionTruth.CANCELLED, ExecutionTruth.REJECTED, ExecutionTruth.SUPPRESSED}
            if prior.execution_state in terminal and truth.reconciliation_generation == prior.reconciliation_generation:
                raise TruthConflict("TERMINAL_STATE_REGRESSION")
        settled = self.store.apply_execution_truth(truth)
        self.store.save_truth_cursor(truth)
        self._seen[truth.reservation_id] = truth
        return settled
