from __future__ import annotations
from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from uuid import UUID, uuid5

_NS = UUID("6dc5cb1b-4c98-55a7-bcca-f9ca2b64c665")

class LifecycleState(StrEnum):
    PAPER="PAPER"; SHADOW="SHADOW"; LIVE_PROBATION="LIVE_PROBATION"
    LIVE_LIMITED="LIVE_LIMITED"; LIVE_FULL="LIVE_FULL"; QUARANTINED="QUARANTINED"; RETIRED="RETIRED"

class RiskDisposition(StrEnum):
    ALLOW="ALLOW"; REDUCE="REDUCE"; REJECT="REJECT"

class RiskDirection(StrEnum):
    RISK_INCREASING="RISK_INCREASING"; RISK_NEUTRAL="RISK_NEUTRAL"; RISK_REDUCING="RISK_REDUCING"

class ReservationState(StrEnum):
    RESERVED="RESERVED"; PARTIALLY_CONSUMED="PARTIALLY_CONSUMED"; CONSUMED="CONSUMED"
    RELEASED="RELEASED"; UNKNOWN_EXECUTION="UNKNOWN_EXECUTION"

def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")

def _nonnegative(value: Decimal, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")

def _canonical(value):
    if isinstance(value, (Decimal, UUID)): return str(value)
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, StrEnum): return value.value
    if isinstance(value, frozenset): return sorted(value)
    return value

@dataclass(frozen=True, slots=True)
class CapitalMandate:
    mandate_id: str
    strategy_id: UUID
    strategy_version: str
    portfolio_id: str
    lifecycle_state: LifecycleState
    execution_authority: frozenset[str]
    permitted_venues: frozenset[str]
    permitted_instruments: frozenset[str]
    capital_allocation: Decimal
    max_gross_exposure: Decimal
    max_net_exposure: Decimal
    max_position_exposure: Decimal
    max_order_notional: Decimal
    max_leverage: Decimal
    max_margin_usage: Decimal
    daily_loss_limit: Decimal
    drawdown_limit: Decimal
    turnover_limit: Decimal
    order_rate_limit: int
    concentration_limit: Decimal
    protected_reserve: Decimal
    valid_from: datetime
    valid_until: datetime
    review_at: datetime
    issuer: str
    version: int
    reason_evidence_ref: str
    policy_version: str
    parent_mandate_id: str | None = None
    schema_version: str = "eqs-capital-mandate-v1"

    def __post_init__(self) -> None:
        for name in ("valid_from", "valid_until", "review_at"): _aware(getattr(self, name), name)
        if self.valid_until <= self.valid_from: raise ValueError("valid_until must be after valid_from")
        if self.version < 1 or self.order_rate_limit < 0: raise ValueError("invalid mandate version/rate")
        for name in ("capital_allocation","max_gross_exposure","max_net_exposure","max_position_exposure",
                     "max_order_notional","max_leverage","max_margin_usage","daily_loss_limit","drawdown_limit",
                     "turnover_limit","concentration_limit","protected_reserve"):
            _nonnegative(getattr(self, name), name)

    def fingerprint(self) -> str:
        payload = {f.name: _canonical(getattr(self, f.name)) for f in fields(self)}
        return sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

@dataclass(frozen=True, slots=True)
class RiskDecisionV1:
    decision_id: UUID
    intent_id: UUID
    strategy_id: UUID
    mandate_id: str
    mandate_version: int
    input_state_ref: str
    disposition: RiskDisposition
    direction: RiskDirection
    requested_quantity: Decimal
    authorised_quantity: Decimal
    reason_codes: tuple[str, ...]
    limits_evaluated: tuple[str, ...]
    limits_consumed: tuple[str, ...]
    decided_at: datetime
    risk_engine_version: str
    policy_version: str
    schema_version: str = "eqs-risk-decision-v1"

    def __post_init__(self) -> None:
        _aware(self.decided_at, "decided_at")
        _nonnegative(self.requested_quantity, "requested_quantity")
        _nonnegative(self.authorised_quantity, "authorised_quantity")
        if self.authorised_quantity > self.requested_quantity:
            raise ValueError("authorised quantity cannot exceed requested quantity")
        if self.disposition == RiskDisposition.REJECT and self.authorised_quantity != 0:
            raise ValueError("rejected decision must authorise zero")

    @classmethod
    def create(cls, **kwargs):
        raw = "|".join(map(str, (kwargs["intent_id"], kwargs["mandate_id"], kwargs["mandate_version"],
                                kwargs["input_state_ref"], kwargs["disposition"], kwargs["authorised_quantity"],
                                kwargs["policy_version"])))
        return cls(decision_id=uuid5(_NS, raw), **kwargs)

@dataclass(frozen=True, slots=True)
class RiskReservation:
    reservation_id: UUID
    decision_id: UUID
    intent_id: UUID
    portfolio_id: str
    strategy_id: UUID
    instrument: str
    venue: str
    reserved_notional: Decimal
    consumed_notional: Decimal
    state: ReservationState
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    version: int = 1
    schema_version: str = "eqs-risk-reservation-v1"

    def __post_init__(self) -> None:
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")
        if self.expires_at is not None: _aware(self.expires_at, "expires_at")
        _nonnegative(self.reserved_notional, "reserved_notional")
        _nonnegative(self.consumed_notional, "consumed_notional")
        if self.consumed_notional > self.reserved_notional:
            raise ValueError("consumed exceeds reserved")
        if self.state == ReservationState.UNKNOWN_EXECUTION and self.reserved_notional == 0:
            raise ValueError("unknown execution must retain reservation")
