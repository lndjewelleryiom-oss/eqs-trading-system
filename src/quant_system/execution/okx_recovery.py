from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from quant_system.execution.adapter_contract import NormalizedExecutionState as S, PrivateReadSnapshot
from quant_system.execution.broker import VenueOrderStatus
from quant_system.execution.lifecycle import DurableExecutionLedger
from quant_system.execution.private_reconciliation import PrivateStateReconciler, ReconciliationAction

@dataclass(frozen=True,slots=True)
class RecoveryEvidence:
    intent_id: UUID
    target_state: S|None
    resolved: bool
    reasons: tuple[str,...]
    venue_order_id: str|None

class OkxLifecycleRecovery:
    """Resolve ambiguous execution only from fresh authenticated private evidence."""
    def __init__(self,ledger:DurableExecutionLedger,*,max_age_seconds:float=15):
        self.ledger=ledger;self.reconciler=PrivateStateReconciler(max_age_seconds=max_age_seconds)
    def recover(self,*,intent_id:UUID,snapshot:PrivateReadSnapshot,expected_open_order_ids:set[UUID],expected_positions:dict[str,Decimal],now:datetime):
        current=self.ledger.get(intent_id)
        if current is None:return RecoveryEvidence(intent_id,None,False,("EXECUTION_NOT_FOUND",),None)
        if current.state!=S.RECONCILIATION_REQUIRED:return RecoveryEvidence(intent_id,None,False,("EXECUTION_NOT_RECONCILIATION_REQUIRED",),current.venue_order_id)
        account=self.reconciler.reconcile(snapshot=snapshot,expected_open_order_ids=expected_open_order_ids,expected_positions=expected_positions,now=now)
        if account.action==ReconciliationAction.HALT:return RecoveryEvidence(intent_id,None,False,account.reasons,current.venue_order_id)
        orders=[o for o in snapshot.open_orders if o.client_order_id==intent_id]
        fills=[f for f in snapshot.recent_fills if getattr(f,"client_order_id","")==str(intent_id)]
        if len(orders)>1:return RecoveryEvidence(intent_id,None,False,("CONTRADICTORY_DUPLICATE_VENUE_ORDERS",),None)
        if orders:
            o=orders[0]
            mapping={VenueOrderStatus.ACKNOWLEDGED:S.ACKNOWLEDGED,VenueOrderStatus.PARTIAL:S.PARTIALLY_FILLED,VenueOrderStatus.FILLED:S.FILLED,VenueOrderStatus.CANCELED:S.CANCELLED,VenueOrderStatus.REJECTED:S.REJECTED}
            target=mapping.get(o.status)
            if target is None:return RecoveryEvidence(intent_id,None,False,("UNSUPPORTED_VENUE_ORDER_STATE",),o.venue_order_id)
            if fills and sum((f.quantity for f in fills),Decimal("0"))>o.quantity:return RecoveryEvidence(intent_id,None,False,("FILL_QUANTITY_EXCEEDS_ORDER",),o.venue_order_id)
            self.ledger.transition(intent_id,target,now=now,reason="AUTHENTICATED_OKX_ORDER_EVIDENCE",venue_order_id=o.venue_order_id)
            return RecoveryEvidence(intent_id,target,True,("AUTHENTICATED_OKX_ORDER_EVIDENCE",),o.venue_order_id)
        if fills:
            venue_ids={f.order_id for f in fills}
            if len(venue_ids)!=1:return RecoveryEvidence(intent_id,None,False,("CONTRADICTORY_FILL_ORDER_IDS",),None)
            self.ledger.transition(intent_id,S.FILLED,now=now,reason="AUTHENTICATED_OKX_FILL_EVIDENCE",venue_order_id=next(iter(venue_ids)))
            return RecoveryEvidence(intent_id,S.FILLED,True,("AUTHENTICATED_OKX_FILL_EVIDENCE",),next(iter(venue_ids)))
        return RecoveryEvidence(intent_id,None,False,("INSUFFICIENT_EXCHANGE_EVIDENCE",),current.venue_order_id)
