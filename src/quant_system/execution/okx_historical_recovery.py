from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from quant_system.execution.adapter_contract import NormalizedExecutionState as S,PrivateReadSnapshot
from quant_system.execution.lifecycle import DurableExecutionLedger
from quant_system.execution.okx_history import OkxHistoricalEvidence
from quant_system.execution.okx_recovery import OkxLifecycleRecovery,RecoveryEvidence

@dataclass(frozen=True,slots=True)
class HistoricalRecoveryDecision:
    resolved: bool
    target_state: S|None
    reasons: tuple[str,...]
    evidence_ids: tuple[str,...]

class OkxHistoricalRecovery:
    def __init__(self,ledger:DurableExecutionLedger,max_age_seconds=30):
        self.ledger=ledger;self.live=OkxLifecycleRecovery(ledger,max_age_seconds=max_age_seconds)
    def recover(self,*,intent_id:UUID,current_snapshot:PrivateReadSnapshot,history:OkxHistoricalEvidence,expected_open_order_ids:set[UUID],expected_positions:dict[str,Decimal],now:datetime):
        if not history.complete:return HistoricalRecoveryDecision(False,None,("HISTORICAL_COVERAGE_INCOMPLETE",),())
        live=self.live.recover(intent_id=intent_id,snapshot=current_snapshot,expected_open_order_ids=expected_open_order_ids,expected_positions=expected_positions,now=now)
        if live.resolved:return HistoricalRecoveryDecision(True,live.target_state,live.reasons,(live.venue_order_id or "",))
        blocking=set(live.reasons)-{"INSUFFICIENT_EXCHANGE_EVIDENCE"}
        if blocking:return HistoricalRecoveryDecision(False,None,live.reasons,())
        orders=[o for o in history.orders if o.client_order_id==intent_id]
        fills=[f for f in history.fills if f.client_order_id==str(intent_id)]
        if len({o.venue_order_id for o in orders})>1:return HistoricalRecoveryDecision(False,None,("CONTRADICTORY_HISTORICAL_ORDER_IDS",),tuple(o.venue_order_id for o in orders))
        if orders:
            o=orders[0]
            from quant_system.execution.broker import VenueOrderStatus
            mapping={VenueOrderStatus.ACKNOWLEDGED:S.ACKNOWLEDGED,VenueOrderStatus.PARTIAL:S.PARTIALLY_FILLED,VenueOrderStatus.FILLED:S.FILLED,VenueOrderStatus.CANCELED:S.CANCELLED,VenueOrderStatus.REJECTED:S.REJECTED}
            target=mapping[o.status]
            if fills and any(f.order_id!=o.venue_order_id for f in fills):return HistoricalRecoveryDecision(False,None,("ORDER_FILL_ID_CONTRADICTION",),())
            if sum((f.quantity for f in fills),Decimal("0"))>o.quantity:return HistoricalRecoveryDecision(False,None,("FILL_QUANTITY_EXCEEDS_ORDER",),())
            self.ledger.transition(intent_id,target,now=now,reason="AUTHENTICATED_OKX_HISTORY_EVIDENCE",venue_order_id=o.venue_order_id)
            return HistoricalRecoveryDecision(True,target,("AUTHENTICATED_OKX_HISTORY_EVIDENCE",),(o.venue_order_id,)+tuple(f.trade_id for f in fills))
        if fills:
            ids={f.order_id for f in fills}
            if len(ids)!=1:return HistoricalRecoveryDecision(False,None,("CONTRADICTORY_FILL_ORDER_IDS",),())
            oid=next(iter(ids));self.ledger.transition(intent_id,S.FILLED,now=now,reason="AUTHENTICATED_OKX_FILL_HISTORY_EVIDENCE",venue_order_id=oid)
            return HistoricalRecoveryDecision(True,S.FILLED,("AUTHENTICATED_OKX_FILL_HISTORY_EVIDENCE",),(oid,)+tuple(f.trade_id for f in fills))
        return HistoricalRecoveryDecision(False,None,("INSUFFICIENT_EXCHANGE_EVIDENCE",),())
