from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal

from quant_system.execution.models import OrderRequest
from quant_system.risk.authority import PreTradeAuthorityEngine, PreTradeRiskState
from quant_system.risk.contracts import CapitalMandate, RiskDecisionV1, RiskDirection, RiskDisposition, RiskReservation
from quant_system.risk.reservations import RiskReservationStore, ReservationConflict

@dataclass(frozen=True, slots=True)
class CapitalControlResult:
    decision: RiskDecisionV1
    reservation: RiskReservation | None

class CapitalControlService:
    """PAPER/SHADOW authority + durable reservation. Never submits execution."""

    def __init__(self, store: RiskReservationStore):
        self.store = store
        self.authority = PreTradeAuthorityEngine()

    def authorise_and_reserve(self, order: OrderRequest, mandate: CapitalMandate,
                              state: PreTradeRiskState) -> CapitalControlResult:
        # Re-read durable reservations so caller cannot omit already committed capacity.
        durable = self.store.active_notional(state.portfolio_id)
        effective = state if durable <= (state.reserved_notional or Decimal("0")) else __import__(
            "dataclasses").replace(state, reserved_notional=durable)
        decision = self.authority.evaluate(order, mandate, effective)
        if decision.disposition == RiskDisposition.REJECT:
            return CapitalControlResult(decision, None)
        if decision.direction == RiskDirection.RISK_REDUCING:
            # Reductions do not consume new-exposure capital; EQS-07 still owns execution truth.
            return CapitalControlResult(decision, None)
        max_reserved = max(
            Decimal("0"),
            mandate.max_gross_exposure
            - (effective.gross_exposure or Decimal("0"))
            - (effective.open_order_notional or Decimal("0")),
        )
        try:
            reservation = self.store.reserve(
                decision, portfolio_id=mandate.portfolio_id, instrument=effective.instrument,
                venue=effective.venue, reference_price=order.reference_price,
                max_total_reserved=max_reserved, now=order.decision_time)
        except ReservationConflict:
            # Concurrent winner consumed the headroom after evaluation: fail closed.
            rejected = RiskDecisionV1.create(
                intent_id=order.order_id, strategy_id=order.strategy_id,
                mandate_id=mandate.mandate_id, mandate_version=mandate.version,
                input_state_ref=effective.fingerprint(), disposition=RiskDisposition.REJECT,
                direction=decision.direction, requested_quantity=order.quantity,
                authorised_quantity=Decimal("0"), reason_codes=("CONCURRENT_CAPACITY_CONFLICT",),
                limits_evaluated=decision.limits_evaluated,
                limits_consumed=decision.limits_consumed, decided_at=order.decision_time,
                risk_engine_version=decision.risk_engine_version,
                policy_version=decision.policy_version)
            return CapitalControlResult(rejected, None)
        return CapitalControlResult(decision, reservation)
