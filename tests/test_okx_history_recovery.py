from datetime import datetime,timezone
from decimal import Decimal
from uuid import uuid4
from quant_system.execution.adapter_contract import PrivateReadSnapshot,NormalizedExecutionState as S
from quant_system.execution.lifecycle import DurableExecutionLedger
from quant_system.execution.okx_history import OkxHistoryReader,OkxHistoricalEvidence
from quant_system.execution.okx_historical_recovery import OkxHistoricalRecovery
from quant_system.execution.okx_private import OkxFill
from quant_system.execution.broker import VenueOrder,VenueOrderStatus
NOW=datetime(2026,9,26,12,tzinfo=timezone.utc)
def pending(l,i):
 l.reserve(i,now=NOW)
 for s in (S.VALIDATED,S.RISK_AUTHORISED,S.ROUTED,S.SUBMISSION_PENDING,S.UNKNOWN,S.RECONCILIATION_REQUIRED):l.transition(i,s,now=NOW,reason="T")
def test_history_reader_deduplicates_and_is_read_only():
 calls=[]
 def get(path,p):
  calls.append((path,p.copy()))
  if "orders-history" in path:return {"code":"0","data":[{"clOrdId":str(I),"ordId":"9","instId":"BTC-USDT-SWAP","state":"filled","sz":"2","accFillSz":"2"}]}
  return {"code":"0","data":[{"tradeId":"T1","clOrdId":str(I),"ordId":"9","instId":"BTC-USDT-SWAP","side":"buy","fillSz":"2","fillPx":"10","fillTime":"1790414400000"}]}
 global I;I=uuid4();r=OkxHistoryReader(get).evidence(instrument="BTC-USDT-SWAP")
 assert r.complete and len(r.orders)==1 and len(r.fills)==1
 assert all(x[1]["instType"]=="SWAP" for x in calls)
def test_history_resolves_missing_current_order(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();pending(l,i)
 o=VenueOrder(i,"9","BTC",VenueOrderStatus.FILLED,Decimal("2"),Decimal("2"))
 f=OkxFill("T1","9",str(i),"BTC","BUY",Decimal("2"),Decimal("10"),NOW)
 h=OkxHistoricalEvidence((o,),(f,),NOW,True,("COMPLETE",))
 r=OkxHistoricalRecovery(l).recover(intent_id=i,current_snapshot=PrivateReadSnapshot(NOW,{"BTC":Decimal("2")},(),()),history=h,expected_open_order_ids=set(),expected_positions={"BTC":Decimal("2")},now=NOW)
 assert r.resolved and r.target_state==S.FILLED and l.get(i).state==S.FILLED
def test_history_contradiction_stays_blocked(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();pending(l,i)
 o=VenueOrder(i,"9","BTC",VenueOrderStatus.FILLED,Decimal("1"),Decimal("1"))
 f=OkxFill("T1","DIFFERENT",str(i),"BTC","BUY",Decimal("1"),Decimal("10"),NOW)
 h=OkxHistoricalEvidence((o,),(f,),NOW,True,("COMPLETE",))
 r=OkxHistoricalRecovery(l).recover(intent_id=i,current_snapshot=PrivateReadSnapshot(NOW,{"BTC":Decimal("1")},(),()),history=h,expected_open_order_ids=set(),expected_positions={"BTC":Decimal("1")},now=NOW)
 assert not r.resolved and l.exposure_blocked() and "ORDER_FILL_ID_CONTRADICTION" in r.reasons
def test_incomplete_pagination_never_resolves(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();pending(l,i)
 h=OkxHistoricalEvidence((),(),NOW,False,("PAGINATION_INCOMPLETE",))
 r=OkxHistoricalRecovery(l).recover(intent_id=i,current_snapshot=PrivateReadSnapshot(NOW,{},(),()),history=h,expected_open_order_ids=set(),expected_positions={},now=NOW)
 assert not r.resolved and r.reasons==("HISTORICAL_COVERAGE_INCOMPLETE",)
def test_history_reader_has_no_mutation_methods():
 r=OkxHistoryReader(lambda p,q:{"code":"0","data":[]})
 for n in ("submit_order","cancel_order","amend_order","withdraw","transfer"):assert not hasattr(r,n)
