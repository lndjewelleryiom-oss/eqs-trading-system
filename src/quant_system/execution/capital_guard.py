from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from quant_system.execution.adapter_contract import NormalizedExecutionState
from quant_system.execution.models import OrderRequest
from quant_system.risk.execution_bridge import CapitalExecutionAuthorityV1

class CapitalAuthorityError(RuntimeError):
    pass

@dataclass(frozen=True, slots=True)
class GuardedExecution:
    authority: CapitalExecutionAuthorityV1
    order: OrderRequest
    state: NormalizedExecutionState

class CapitalGuard:
    """EQS-07 side validation. This object does not submit to a venue."""

    @staticmethod
    def validate(authority: CapitalExecutionAuthorityV1, order: OrderRequest,
                 *, now: datetime) -> GuardedExecution:
        reasons=[]
        if authority.intent_id != order.order_id: reasons.append("INTENT_ID_MISMATCH")
        if authority.strategy_id != order.strategy_id: reasons.append("STRATEGY_ID_MISMATCH")
        if authority.instrument != order.symbol: reasons.append("INSTRUMENT_MISMATCH")
        if authority.side != order.side or authority.order_type != order.order_type:
            reasons.append("ORDER_SHAPE_MISMATCH")
        if order.quantity > authority.authorised_quantity:
            reasons.append("AUTHORISED_QUANTITY_EXCEEDED")
        if authority.authority_expires_at is not None and now > authority.authority_expires_at:
            reasons.append("CAPITAL_AUTHORITY_EXPIRED")
        if authority.execution_mode not in {"PAPER","SHADOW"}:
            reasons.append("LIVE_CAPITAL_DISABLED")
        if reasons:
            raise CapitalAuthorityError(",".join(reasons))
        return GuardedExecution(authority, order, NormalizedExecutionState.RISK_AUTHORISED)
class SubmissionLifecycle:
    """Synthetic/durable-state model; exchange submission remains EQS-07 owned."""
    @staticmethod
    def before_submit(guarded: GuardedExecution) -> GuardedExecution:
        if guarded.state != NormalizedExecutionState.RISK_AUTHORISED:
            raise CapitalAuthorityError("INVALID_PRE_SUBMISSION_STATE")
        return GuardedExecution(guarded.authority, guarded.order,
                                NormalizedExecutionState.SUBMISSION_PENDING)

    @staticmethod
    def ambiguous(guarded: GuardedExecution) -> GuardedExecution:
        if guarded.state != NormalizedExecutionState.SUBMISSION_PENDING:
            raise CapitalAuthorityError("AMBIGUITY_REQUIRES_SUBMISSION_PENDING")
        return GuardedExecution(guarded.authority, guarded.order,
                                NormalizedExecutionState.RECONCILIATION_REQUIRED)
