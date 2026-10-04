from datetime import datetime,timezone
from uuid import uuid4
import pytest
from quant_system.execution.adapter_contract import NormalizedExecutionState as S
from quant_system.execution.lifecycle import DurableExecutionLedger,ExecutionBlocked,InvalidExecutionTransition
NOW=datetime(2026,9,26,11,0,tzinfo=timezone.utc)
def routed(ledger,i):
    ledger.reserve(i,now=NOW)
    for state in (S.VALIDATED,S.RISK_AUTHORISED,S.ROUTED):ledger.transition(i,state,now=NOW,reason="TEST")
def test_duplicate_reservation_returns_same_execution(tmp_path):
    l=DurableExecutionLedger(tmp_path/"r.db");i=uuid4()
    first=l.reserve(i,now=NOW);second=l.reserve(i,now=NOW)
    assert first==second and first.intent_id==i
    with l._connect() as db: assert db.execute("select count(*) n from execution_lifecycle").fetchone()["n"]==1
def test_submission_pending_is_durable_and_blocks_new_exposure(tmp_path):
    l=DurableExecutionLedger(tmp_path/"r.db");i=uuid4();routed(l,i)
    l.transition(i,S.SUBMISSION_PENDING,now=NOW,reason="PRE_SUBMISSION_DURABLE_BARRIER")
    assert l.get(i).state==S.SUBMISSION_PENDING and l.exposure_blocked()
    with pytest.raises(ExecutionBlocked):l.assert_new_exposure_allowed()
def test_unclean_restart_forces_unknown_then_reconciliation_required(tmp_path):
    path=tmp_path/"r.db";l=DurableExecutionLedger(path);i=uuid4();routed(l,i)
    l.transition(i,S.SUBMISSION_PENDING,now=NOW,reason="PRE_SUBMISSION_DURABLE_BARRIER")
    restarted=DurableExecutionLedger(path);recovered=restarted.recover_after_restart(now=NOW)
    assert recovered[0].state==S.RECONCILIATION_REQUIRED
    assert restarted.get(i).state==S.RECONCILIATION_REQUIRED and restarted.exposure_blocked()
    with restarted._connect() as db:
        states=[r["state"] for r in db.execute("select state from execution_transitions where intent_id=? order by id",(str(i),))]
    assert S.UNKNOWN.value in states and S.RECONCILIATION_REQUIRED.value in states
def test_ambiguous_submit_is_explicit_unknown_then_reconciliation(tmp_path):
    l=DurableExecutionLedger(tmp_path/"r.db");i=uuid4();routed(l,i)
    l.transition(i,S.SUBMISSION_PENDING,now=NOW,reason="BARRIER")
    l.transition(i,S.UNKNOWN,now=NOW,reason="TRANSPORT_AMBIGUOUS")
    l.transition(i,S.RECONCILIATION_REQUIRED,now=NOW,reason="EXCHANGE_TRUTH_REQUIRED")
    assert l.exposure_blocked()
def test_reconciliation_can_resolve_to_acknowledged_and_unblock(tmp_path):
    l=DurableExecutionLedger(tmp_path/"r.db");i=uuid4();routed(l,i)
    l.transition(i,S.SUBMISSION_PENDING,now=NOW,reason="BARRIER")
    l.transition(i,S.UNKNOWN,now=NOW,reason="AMBIGUOUS")
    l.transition(i,S.RECONCILIATION_REQUIRED,now=NOW,reason="TRUTH_REQUIRED")
    l.transition(i,S.ACKNOWLEDGED,now=NOW,reason="AUTHENTICATED_EXCHANGE_MATCH",venue_order_id="OKX-1")
    assert not l.exposure_blocked() and l.get(i).venue_order_id=="OKX-1"
def test_illegal_state_jump_is_rejected(tmp_path):
    l=DurableExecutionLedger(tmp_path/"r.db");i=uuid4();l.reserve(i,now=NOW)
    with pytest.raises(InvalidExecutionTransition):l.transition(i,S.FILLED,now=NOW,reason="BAD_JUMP")
def test_ledger_has_no_exchange_mutation_surface(tmp_path):
    l=DurableExecutionLedger(tmp_path/"r.db")
    for name in ("submit_order","cancel_order","amend_order","withdraw","transfer"):assert not hasattr(l,name)
