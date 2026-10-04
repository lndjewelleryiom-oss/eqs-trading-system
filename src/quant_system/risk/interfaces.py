from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

class ExecutionTruth(StrEnum):
    OPEN="OPEN"; PARTIAL_FILL="PARTIAL_FILL"; FILLED="FILLED"
    CANCELLED="CANCELLED"; REJECTED="REJECTED"; UNKNOWN="UNKNOWN"
    SUPPRESSED="SUPPRESSED"

@dataclass(frozen=True, slots=True)
class EQS07ExecutionTruthV1:
    reservation_id: UUID
    execution_state: ExecutionTruth
    cumulative_filled_notional: Decimal
    observed_at: datetime
    evidence_ref: str
    schema_version: str = "eqs06-eqs07-execution-truth-v1"

@dataclass(frozen=True, slots=True)
class EQS08LifecycleInputV1:
    strategy_id: UUID
    lifecycle_state: str
    effective_at: datetime
    evidence_ref: str
    schema_version: str = "eqs06-eqs08-lifecycle-input-v1"

@dataclass(frozen=True, slots=True)
class EQS09SafetyInputV1:
    global_kill: bool
    portfolio_kill: bool
    strategy_kill: bool
    effective_at: datetime
    evidence_ref: str
    schema_version: str = "eqs06-eqs09-safety-input-v1"
