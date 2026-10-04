from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid5, UUID

from quant_system.backtest.events import BarEvent
from quant_system.paper.engine import PaperTradingEngine, PaperFillRecord
from quant_system.execution.capital_guard import CapitalGuard, SubmissionLifecycle
from quant_system.execution.models import OrderRequest
from quant_system.risk.execution_bridge import CapitalExecutionAuthorityV1, EQS07ExecutionTruthV2, ExecutionTruthConsumer
from quant_system.risk.interfaces import ExecutionTruth

_EXEC_NS=UUID("a70b45bd-03b5-54aa-886d-a7b35f38303a")

@dataclass(frozen=True, slots=True)
class PaperCapitalState:
    cumulative_filled_notional: Decimal
    truth_sequence: int
    reconciliation_generation: int

class PaperCapitalExecutionPath:
    """PAPER handoff/fill/truth settlement. No venue submission exists."""

    def __init__(self, engine: PaperTradingEngine, truth: ExecutionTruthConsumer):
        self.engine=engine; self.truth=truth
        self._states: dict[UUID,PaperCapitalState]={}

    def submit(self, order: OrderRequest, authority: CapitalExecutionAuthorityV1) -> None:
        guarded=CapitalGuard.validate(authority,order,now=order.decision_time)
        SubmissionLifecycle.before_submit(guarded)
        self.engine.submit(order)
        self._states[authority.reservation_id]=PaperCapitalState(Decimal("0"),0,0)

    def on_bar(self, bar: BarEvent, authority: CapitalExecutionAuthorityV1):
        fills=self.engine.on_bar(bar)
        relevant=tuple(f for f in fills if f.order.order_id==authority.intent_id)
        for record in relevant:
            self._apply_fill(record,authority)
        return relevant
    def _apply_fill(self, record: PaperFillRecord, authority: CapitalExecutionAuthorityV1):
        state=self._states.get(authority.reservation_id,PaperCapitalState(Decimal("0"),0,0))
        cumulative=state.cumulative_filled_notional + record.fill.quantity * authority.reference_price
        sequence=state.truth_sequence+1
        filled_qty=sum((f.fill.quantity for f in self.engine.fills
                        if f.order.order_id==authority.intent_id),Decimal("0"))
        status=ExecutionTruth.FILLED if filled_qty >= authority.authorised_quantity else ExecutionTruth.PARTIAL_FILL
        event=EQS07ExecutionTruthV2(
            authority.reservation_id,uuid5(_EXEC_NS,str(authority.intent_id)),
            authority.intent_id,authority.venue,authority.instrument,status,cumulative,
            sequence,state.reconciliation_generation,record.fill.timestamp,
            f"PAPER_FILL:{record.fill.order_id}:{sequence}")
        self.truth.apply(event)
        self._states[authority.reservation_id]=PaperCapitalState(
            cumulative,sequence,state.reconciliation_generation)

    def export_state(self) -> dict[UUID,PaperCapitalState]:
        return dict(self._states)

    def restore_state(self, states: dict[UUID,PaperCapitalState]) -> None:
        self._states=dict(states)

    def recover_from_truth_cursor(self, authority: CapitalExecutionAuthorityV1) -> None:
        cursor=self.truth.store.truth_cursor(authority.reservation_id)
        if cursor is None:
            self._states.setdefault(authority.reservation_id,PaperCapitalState(Decimal("0"),0,0))
            return
        self._states[authority.reservation_id]=PaperCapitalState(
            Decimal(cursor["cumulative_filled_notional"]),int(cursor["truth_sequence"]),
            int(cursor["reconciliation_generation"]))
