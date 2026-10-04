from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4
from quant_system.execution.adapter_contract import PrivateReadSnapshot,NormalizedExecutionState as S
from quant_system.execution.broker import VenueOrder,VenueOrderStatus
from quant_system.execution.lifecycle import DurableExecutionLedger
from quant_system.execution.okx_private import OkxFill
from quant_system.execution.okx_recovery import OkxLifecycleRecovery
NOW=datetime(2026,9,26,11,30,tzinfo=timezone.utc)
def pending(l,i):
 l.reserve(i,now=NOW)
 for s in (S.VALIDATED,S.RISK_AUTHORISED,S.ROUTED,S.SUBMISSION_PENDING,S.UNKNOWN,S.RECONCILIATION_REQUIRED):l.transition(i,s,now=NOW,reason="TEST")
def test_matching_open_order_resolves_acknowledged(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();pending(l,i)
 o=VenueOrder(i,"OKX-9","BTC",VenueOrderStatus.ACKNOWLEDGED,Decimal("1"))
 snap=PrivateReadSnapshot(NOW,{},(o,))
 r=OkxLifecycleRecovery(l).recover(intent_id=i,snapshot=snap,expected_open_order_ids={i},expected_positions={},now=NOW)
 assert r.resolved and l.get(i).state==S.ACKNOWLEDGED and not l.exposure_blocked()
def test_fill_evidence_resolves_filled(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();pending(l,i)
 f=OkxFill("t","V1",str(i),"BTC","BUY",Decimal("1"),Decimal("10"),NOW)
 snap=PrivateReadSnapshot(NOW,{"BTC":Decimal("1")},(),(f,))
 r=OkxLifecycleRecovery(l).recover(intent_id=i,snapshot=snap,expected_open_order_ids=set(),expected_positions={"BTC":Decimal("1")},now=NOW)
 assert r.resolved and l.get(i).state==S.FILLED and l.get(i).venue_order_id=="V1"
def test_stale_evidence_keeps_blocked(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();pending(l,i)
 snap=PrivateReadSnapshot(NOW-timedelta(seconds=16),{},(),())
 r=OkxLifecycleRecovery(l,max_age_seconds=15).recover(intent_id=i,snapshot=snap,expected_open_order_ids=set(),expected_positions={},now=NOW)
 assert not r.resolved and "PRIVATE_STATE_STALE" in r.reasons and l.get(i).state==S.RECONCILIATION_REQUIRED and l.exposure_blocked()
def test_account_contradiction_keeps_blocked(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();pending(l,i)
 o=VenueOrder(i,"OKX-9","BTC",VenueOrderStatus.ACKNOWLEDGED,Decimal("1"))
 snap=PrivateReadSnapshot(NOW,{"BTC":Decimal("2")},(o,))
 r=OkxLifecycleRecovery(l).recover(intent_id=i,snapshot=snap,expected_open_order_ids={i},expected_positions={"BTC":Decimal("1")},now=NOW)
 assert not r.resolved and "POSITION_MISMATCH" in r.reasons and l.exposure_blocked()
def test_absence_of_order_and_fill_is_not_treated_as_rejection(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();pending(l,i)
 r=OkxLifecycleRecovery(l).recover(intent_id=i,snapshot=PrivateReadSnapshot(NOW,{},(),()),expected_open_order_ids=set(),expected_positions={},now=NOW)
 assert not r.resolved and r.reasons==("INSUFFICIENT_EXCHANGE_EVIDENCE",) and l.get(i).state==S.RECONCILIATION_REQUIRED
def test_resolver_has_no_exchange_mutation_surface(tmp_path):
 r=OkxLifecycleRecovery(DurableExecutionLedger(tmp_path/"x.db"))
 for n in ("submit_order","cancel_order","amend_order","withdraw","transfer"):assert not hasattr(r,n)
