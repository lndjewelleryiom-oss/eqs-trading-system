from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4
from quant_system.execution.bybit_private import BybitLinearPrivateReadAdapter
from quant_system.execution.bybit_history import BybitHistoryReader,BybitHistoricalEvidence
from quant_system.execution.bybit_recovery import BybitHistoricalRecovery
from quant_system.execution.adapter_contract import PrivateReadSnapshot,NormalizedExecutionState as S
from quant_system.execution.lifecycle import DurableExecutionLedger
from quant_system.execution.audit import ExecutionAuditTrail
NOW=datetime(2026,9,26,14,tzinfo=timezone.utc)
def pending(l,i):
 l.reserve(i,now=NOW)
 for s in (S.VALIDATED,S.RISK_AUTHORISED,S.ROUTED,S.SUBMISSION_PENDING,S.UNKNOWN,S.RECONCILIATION_REQUIRED):l.transition(i,s,now=NOW,reason="T")
def payload(path,p):
 if path=="/v5/position/list":return {"retCode":0,"result":{"list":[{"symbol":"BTCUSDT","side":"Buy","size":"2"}]}}
 if path=="/v5/order/realtime":return {"retCode":0,"result":{"list":[{"orderLinkId":str(I),"orderId":"O1","symbol":"BTCUSDT","orderStatus":"New","qty":"2","cumExecQty":"0"}]}}
 if path=="/v5/execution/list":return {"retCode":0,"result":{"list":[]}}
 return {"retCode":0,"result":{"list":[]}}
def test_private_snapshot_normalizes_positions_orders_and_is_read_only():
 global I;I=uuid4();a=BybitLinearPrivateReadAdapter(payload,clock=lambda:NOW);s=a.private_read()
 assert s.positions["BTCUSDT"]==Decimal("2") and s.open_orders[0].client_order_id==I
 for n in ("withdraw","transfer","amend_order"):assert not hasattr(a,n)
def test_history_cursor_dedup_and_completion():
 global I;I=uuid4();calls=[]
 def get(path,p):
  calls.append((path,p.copy()))
  if path=="/v5/order/history":return {"retCode":0,"result":{"list":[{"orderLinkId":str(I),"orderId":"O1","symbol":"BTCUSDT","orderStatus":"Filled","qty":"2","cumExecQty":"2"}],"nextPageCursor":""}}
  return {"retCode":0,"result":{"list":[{"execId":"E1","orderLinkId":str(I),"orderId":"O1","symbol":"BTCUSDT","side":"Buy","execQty":"2","execPrice":"10","execTime":"1790424000000"}],"nextPageCursor":""}}
 h=BybitHistoryReader(get,clock=lambda:NOW).evidence(instrument="BTCUSDT")
 assert h.complete and len(h.orders)==1 and len(h.fills)==1
 assert {x[0] for x in calls}=={"/v5/order/history","/v5/execution/list"}
def test_history_recovery_resolves_and_audits(tmp_path):
 global I;I=uuid4();l=DurableExecutionLedger(tmp_path/"l.db");pending(l,I)
 row={"orderLinkId":str(I),"orderId":"O1","symbol":"BTCUSDT","orderStatus":"Filled","qty":"2","cumExecQty":"2"}
 fill={"execId":"E1","orderLinkId":str(I),"orderId":"O1","symbol":"BTCUSDT","side":"Buy","execQty":"2","execPrice":"10","execTime":"1790424000000"}
 h=BybitHistoricalEvidence((BybitLinearPrivateReadAdapter._order(row),),(BybitLinearPrivateReadAdapter._fill(fill),),NOW,True,("COMPLETE",))
 audit=ExecutionAuditTrail(tmp_path/"a.db")
 r=BybitHistoricalRecovery(l,audit=audit).recover(intent_id=I,current_snapshot=PrivateReadSnapshot(NOW,{"BTCUSDT":Decimal("2")},(),()),history=h,expected_open_order_ids=set(),expected_positions={"BTCUSDT":Decimal("2")},now=NOW)
 assert r[0] and r[1]==S.FILLED and audit.records(I)[0].evidence["venue"]=="BYBIT_LINEAR"
def test_stale_or_incomplete_or_absent_evidence_stays_blocked(tmp_path):
 global I;I=uuid4();l=DurableExecutionLedger(tmp_path/"l.db");pending(l,I);r=BybitHistoricalRecovery(l,max_age_seconds=15)
 empty=BybitHistoricalEvidence((),(),NOW,True,("COMPLETE",))
 x=r.recover(intent_id=I,current_snapshot=PrivateReadSnapshot(NOW-timedelta(seconds=16),{},(),()),history=empty,expected_open_order_ids=set(),expected_positions={},now=NOW)
 assert not x[0] and l.exposure_blocked()
 y=r.recover(intent_id=I,current_snapshot=PrivateReadSnapshot(NOW,{},(),()),history=BybitHistoricalEvidence((),(),NOW,False,("PAGINATION_INCOMPLETE",)),expected_open_order_ids=set(),expected_positions={},now=NOW)
 assert not y[0] and y[2]==("HISTORICAL_COVERAGE_INCOMPLETE",)
 z=r.recover(intent_id=I,current_snapshot=PrivateReadSnapshot(NOW,{},(),()),history=empty,expected_open_order_ids=set(),expected_positions={},now=NOW)
 assert not z[0] and z[2]==("INSUFFICIENT_EXCHANGE_EVIDENCE",)
