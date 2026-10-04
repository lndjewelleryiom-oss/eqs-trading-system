from __future__ import annotations
from dataclasses import dataclass

from quant_system.execution.capital_guard import CapitalGuard
from quant_system.execution.models import OrderRequest
from quant_system.risk.authority import PreTradeRiskState
from quant_system.risk.contracts import CapitalMandate, RiskDisposition
from quant_system.risk.control import CapitalControlResult, CapitalControlService
from quant_system.risk.execution_bridge import CapitalExecutionAuthorityV1

@dataclass(frozen=True, slots=True)
class NonLiveCapitalAuthorisation:
    control: CapitalControlResult
    execution_authority: CapitalExecutionAuthorityV1 | None
    authorised_order: OrderRequest | None

class NonLiveCapitalBridge:
    """Mandatory EQS-06 boundary for new-risk PAPER/SHADOW orders."""

    def __init__(self, control: CapitalControlService):
        self.control=control

    def authorise(self, order: OrderRequest, mandate: CapitalMandate,
                  state: PreTradeRiskState, *, strategy_version: str,
                  execution_mode: str) -> NonLiveCapitalAuthorisation:
        if execution_mode not in {"PAPER","SHADOW"}:
            raise ValueError("LIVE capital authority is disabled")
        if execution_mode != mandate.lifecycle_state.value or execution_mode not in mandate.execution_authority:
            raise ValueError("EXECUTION_MODE_NOT_AUTHORISED_BY_MANDATE")
        result=self.control.authorise_and_reserve(order,mandate,state)
        if result.decision.disposition == RiskDisposition.REJECT:
            return NonLiveCapitalAuthorisation(result,None,None)
        if result.reservation is None:
            # Proven risk reduction is authorised but consumes no new capital reservation.
            return NonLiveCapitalAuthorisation(result,None,order)
        r=result.reservation
        authority=CapitalExecutionAuthorityV1(
            r.reservation_id,r.decision_id,r.intent_id,mandate.mandate_id,mandate.version,
            order.strategy_id,strategy_version,mandate.portfolio_id,state.venue,state.instrument,
            order.side,order.order_type,order.quantity,result.decision.authorised_quantity,
            order.reference_price,order.limit_price,order.reduce_only,r.reserved_notional,
            order.decision_time,r.expires_at,result.decision.input_state_ref,
            mandate.policy_version,execution_mode)
        guarded_order=type(order)(
            order.strategy_id,order.symbol,order.side,result.decision.authorised_quantity,
            order.order_type,order.decision_time,order.reference_price,order.limit_price,
            order.reduce_only,order.order_id)
        CapitalGuard.validate(authority,guarded_order,now=order.decision_time)
        return NonLiveCapitalAuthorisation(result,authority,guarded_order)
