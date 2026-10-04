from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone
from uuid import uuid4
from quant_system.execution.adapter_contract import NormalizedExecutionState as S
from quant_system.execution.lifecycle import DurableExecutionLedger
NOW=datetime(2026,9,26,13,tzinfo=timezone.utc)
def route(l,i):
 l.reserve(i,now=NOW)
 for s in (S.VALIDATED,S.RISK_AUTHORISED,S.ROUTED):l.transition(i,s,now=NOW,reason="T")
def test_concurrent_duplicate_reservation_creates_one_identity(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4()
 with ThreadPoolExecutor(max_workers=8) as ex:rows=list(ex.map(lambda _:l.reserve(i,now=NOW),range(32)))
 assert {r.intent_id for r in rows}=={i}
 with l._connect() as db:assert db.execute("select count(*) n from execution_lifecycle").fetchone()["n"]==1
def test_repeated_restart_recovery_is_idempotent(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");i=uuid4();route(l,i);l.transition(i,S.SUBMISSION_PENDING,now=NOW,reason="BARRIER")
 assert len(l.recover_after_restart(now=NOW))==1
 assert l.recover_after_restart(now=NOW)==()
 assert l.get(i).state==S.RECONCILIATION_REQUIRED
def test_partial_fill_lifecycle_survives_restart_without_duplicate_identity(tmp_path):
 path=tmp_path/"x.db";l=DurableExecutionLedger(path);i=uuid4();route(l,i)
 l.transition(i,S.SUBMISSION_PENDING,now=NOW,reason="B");l.transition(i,S.ACKNOWLEDGED,now=NOW,reason="A",venue_order_id="9")
 l.transition(i,S.PARTIALLY_FILLED,now=NOW,reason="P")
 restarted=DurableExecutionLedger(path)
 assert restarted.get(i).state==S.PARTIALLY_FILLED
 restarted.transition(i,S.PARTIALLY_FILLED,now=NOW,reason="P2")
 restarted.transition(i,S.FILLED,now=NOW,reason="F")
 assert restarted.get(i).state==S.FILLED
def test_crash_before_submission_pending_never_becomes_ambiguous(tmp_path):
 path=tmp_path/"x.db";l=DurableExecutionLedger(path);i=uuid4();route(l,i)
 restarted=DurableExecutionLedger(path)
 assert restarted.recover_after_restart(now=NOW)==() and restarted.get(i).state==S.ROUTED
def test_crash_after_pending_always_requires_truth(tmp_path):
 path=tmp_path/"x.db";l=DurableExecutionLedger(path);i=uuid4();route(l,i);l.transition(i,S.SUBMISSION_PENDING,now=NOW,reason="B")
 DurableExecutionLedger(path).recover_after_restart(now=NOW)
 assert DurableExecutionLedger(path).get(i).state==S.RECONCILIATION_REQUIRED
