from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import json
from uuid import UUID, uuid5
from quant_system.core.enums import OrderType, Side
from quant_system.execution.models import OrderRequest

_INTENT_NAMESPACE = UUID("30bc1847-4c48-5f72-a034-d74208621c5c")

class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"

@dataclass(frozen=True, slots=True)
class OrderIntent:
    strategy_id: UUID
    strategy_version: str
    portfolio_id: str
    venue: str
    instrument: str
    side: Side
    quantity: object
    order_type: OrderType
    created_at: datetime
    reference_price: object
    signal_ref: str
    risk_decision_ref: str
    capital_mandate_ref: str
    execution_mode: ExecutionMode
    limit_price: object | None = None
    reduce_only: bool = False
    schema_version: str = "eqs-order-intent-v1"
    intent_id: UUID = field(init=False)

    def __post_init__(self):
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        payload = "|".join(map(str,(self.schema_version,self.strategy_id,self.strategy_version,self.portfolio_id,self.venue,self.instrument,self.side,self.quantity,self.order_type,self.created_at.isoformat(),self.signal_ref,self.risk_decision_ref,self.capital_mandate_ref,self.execution_mode,self.limit_price,self.reduce_only)))
        object.__setattr__(self, "intent_id", uuid5(_INTENT_NAMESPACE, payload))

    def to_order_request(self) -> OrderRequest:
        return OrderRequest(self.strategy_id,self.instrument,self.side,self.quantity,self.order_type,self.created_at,self.reference_price,self.limit_price,self.reduce_only,self.intent_id)

@dataclass(frozen=True, slots=True)
class ExecutionAuthority:
    authority_id: str
    strategy_id: UUID
    strategy_version: str
    lifecycle_state: str
    allowed_modes: frozenset[ExecutionMode]
    venues: frozenset[str]
    instruments: frozenset[str]
    capital_mandate_ref: str
    risk_policy_ref: str
    valid_from: datetime
    valid_until: datetime
    review_state: str
    global_kill: bool = False
    venue_kill: bool = False
    instrument_kill: bool = False
    strategy_kill: bool = False
    broker_submission_master_enabled: bool = False

@dataclass(frozen=True, slots=True)
class RouteDecision:
    allowed: bool
    reason_codes: tuple[str,...]
    order: OrderRequest | None

class ExecutionRouter:
    def authorize(self, intent: OrderIntent, authority: ExecutionAuthority | None, *, now: datetime, venue_healthy: bool=True, reconciliation_healthy: bool=True) -> RouteDecision:
        reasons=[]
        if authority is None: reasons.append("AUTHORITY_MISSING")
        else:
            if authority.strategy_id != intent.strategy_id or authority.strategy_version != intent.strategy_version: reasons.append("STRATEGY_AUTHORITY_MISMATCH")
            if intent.execution_mode not in authority.allowed_modes: reasons.append("MODE_NOT_AUTHORISED")
            if intent.venue not in authority.venues: reasons.append("VENUE_NOT_AUTHORISED")
            if intent.instrument not in authority.instruments: reasons.append("INSTRUMENT_NOT_AUTHORISED")
            if authority.capital_mandate_ref != intent.capital_mandate_ref: reasons.append("CAPITAL_MANDATE_MISMATCH")
            if not (authority.valid_from <= now <= authority.valid_until): reasons.append("AUTHORITY_STALE")
            if authority.review_state != "APPROVED": reasons.append("AUTHORITY_NOT_APPROVED")
            if authority.global_kill or authority.venue_kill or authority.instrument_kill or authority.strategy_kill: reasons.append("KILL_ACTIVE")
            if intent.execution_mode == ExecutionMode.LIVE:
                if authority.lifecycle_state not in {"LIVE_PROBATION","LIVE_LIMITED","LIVE_FULL"}: reasons.append("LIVE_LIFECYCLE_NOT_AUTHORISED")
                if not authority.broker_submission_master_enabled: reasons.append("BROKER_SUBMISSION_MASTER_DISABLED")
        if not venue_healthy: reasons.append("VENUE_UNHEALTHY")
        if not reconciliation_healthy: reasons.append("RECONCILIATION_UNHEALTHY")
        return RouteDecision(not reasons, tuple(reasons) or ("AUTHORISED",), None if reasons else intent.to_order_request())
