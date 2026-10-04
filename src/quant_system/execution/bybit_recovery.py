from __future__ import annotations
from datetime import datetime
from decimal import Decimal
from uuid import UUID
from quant_system.execution.adapter_contract import NormalizedExecutionState as S,PrivateReadSnapshot
from quant_system.execution.broker import VenueOrderStatus
from quant_system.execution.lifecycle import DurableExecutionLedger
from quant_system.execution.private_reconciliation import PrivateStateReconciler,ReconciliationAction
from quant_system.execution.bybit_history import BybitHistoricalEvidence
from quant_system.execution.audit import ExecutionAuditTrail
class BybitHistoricalRecovery:
 def __init__(self,ledger:DurableExecutionLedger,*,audit:ExecutionAuditTrail|None=None,max_age_seconds=30):
  self.ledger=ledger;self.audit=audit;self.reconciler=PrivateStateReconciler(max_age_seconds=max_age_seconds)
 def recover(self,*,intent_id:UUID,current_snapshot:PrivateReadSnapshot,history:BybitHistoricalEvidence,expected_open_order_ids:set[UUID],expected_positions:dict[str,Decimal],now:datetime):
  cur=self.ledger.get(intent_id)
  if cur is None or cur.state!=S.RECONCILIATION_REQUIRED:return False,None,("EXECUTION_NOT_RECONCILIATION_REQUIRED",)
  account=self.reconciler.reconcile(snapshot=current_snapshot,expected_open_order_ids=expected_open_order_ids,expected_positions=expected_positions,now=now)
  if account.action==ReconciliationAction.HALT:return False,None,account.reasons
  if not history.complete:return False,None,("HISTORICAL_COVERAGE_INCOMPLETE",)
  orders=[o for o in current_snapshot.open_orders+history.orders if o.client_order_id==intent_id]
  fills=[f for f in current_snapshot.recent_fills+history.fills if getattr(f,"client_order_id","")==str(intent_id)]
  unique_orders={o.venue_order_id:o for o in orders}
  if len(unique_orders)>1:return False,None,("CONTRADICTORY_HISTORICAL_ORDER_IDS",)
  if unique_orders:
   o=next(iter(unique_orders.values()))
   if fills and any(f.order_id!=o.venue_order_id for f in fills):return False,None,("ORDER_FILL_ID_CONTRADICTION",)
   if sum((f.quantity for f in {f.exec_id:f for f in fills}.values()),Decimal("0"))>o.quantity:return False,None,("FILL_QUANTITY_EXCEEDS_ORDER",)
   mapping={VenueOrderStatus.ACKNOWLEDGED:S.ACKNOWLEDGED,VenueOrderStatus.PARTIAL:S.PARTIALLY_FILLED,VenueOrderStatus.FILLED:S.FILLED,VenueOrderStatus.CANCELED:S.CANCELLED,VenueOrderStatus.REJECTED:S.REJECTED}
   target=mapping[o.status];ids=(o.venue_order_id,)+tuple(sorted({f.exec_id for f in fills}))
   self.ledger.transition(intent_id,target,now=now,reason="AUTHENTICATED_BYBIT_EVIDENCE",venue_order_id=o.venue_order_id)
   if self.audit:self.audit.append(intent_id,"BYBIT_RECOVERY",now,{"venue":"BYBIT_LINEAR","target_state":target.value,"evidence_ids":ids})
   return True,target,("AUTHENTICATED_BYBIT_EVIDENCE",)
  if fills:
   ids={f.order_id for f in fills}
   if len(ids)!=1:return False,None,("CONTRADICTORY_FILL_ORDER_IDS",)
   oid=next(iter(ids));self.ledger.transition(intent_id,S.FILLED,now=now,reason="AUTHENTICATED_BYBIT_FILL_EVIDENCE",venue_order_id=oid)
   if self.audit:self.audit.append(intent_id,"BYBIT_RECOVERY",now,{"venue":"BYBIT_LINEAR","target_state":S.FILLED.value,"evidence_ids":tuple(sorted({f.exec_id for f in fills}))})
   return True,S.FILLED,("AUTHENTICATED_BYBIT_FILL_EVIDENCE",)
  return False,None,("INSUFFICIENT_EXCHANGE_EVIDENCE",)
