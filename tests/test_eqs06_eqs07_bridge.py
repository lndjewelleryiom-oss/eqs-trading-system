from datetime import timedelta
from decimal import Decimal
from uuid import uuid4
import pytest
from quant_system.execution.capital_guard import CapitalAuthorityError, CapitalGuard, SubmissionLifecycle
from quant_system.execution.adapter_contract import NormalizedExecutionState
from quant_system.risk.contracts import ReservationState
from quant_system.risk.execution_bridge import CapitalExecutionAuthorityV1, EQS07ExecutionTruthV2, ExecutionTruthConsumer, TruthConflict
from quant_system.risk.interfaces import ExecutionTruth
from quant_system.risk.reservations import RiskReservationStore
from test_risk_control_extensions import _decision
from test_pretrade_authority import NOW, SID, order

def setup(tmp_path):
    store=RiskReservationStore(tmp_path/"bridge.db"); d=_decision()
    r=store.reserve(d,portfolio_id="p",instrument="XYZ",venue="SYNTH",
        reference_price=Decimal("100"),max_total_reserved=Decimal("200"),now=NOW)
    o=order("2")
    o=type(o)(o.strategy_id,o.symbol,o.side,o.quantity,o.order_type,o.decision_time,
              o.reference_price,o.limit_price,o.reduce_only,r.intent_id)
    a=CapitalExecutionAuthorityV1(
        r.reservation_id,r.decision_id,r.intent_id,"m",1,SID,"fixture-v1","p",
        "SYNTH","XYZ",o.side,o.order_type,Decimal("2"),Decimal("2"),Decimal("100"),
        None,False,Decimal("200"),NOW,NOW+timedelta(minutes=1),"state","policy","PAPER")
    return store,r,o,a

def truth(r,seq,state,filled="0",generation=0):
    return EQS07ExecutionTruthV2(
        r.reservation_id,uuid4(),r.intent_id,r.venue,r.instrument,state,
        Decimal(filled),seq,generation,NOW+timedelta(seconds=seq),"synthetic-evidence")

def test_guard_rejects_quantity_escalation(tmp_path):
    _,_,o,a=setup(tmp_path)
    bad=type(o)(o.strategy_id,o.symbol,o.side,Decimal("3"),o.order_type,o.decision_time,
                o.reference_price,o.limit_price,o.reduce_only,o.order_id)
    with pytest.raises(CapitalAuthorityError,match="AUTHORISED_QUANTITY_EXCEEDED"):
        CapitalGuard.validate(a,bad,now=NOW)

def test_submission_pending_then_ambiguity(tmp_path):
    _,_,o,a=setup(tmp_path)
    p=SubmissionLifecycle.before_submit(CapitalGuard.validate(a,o,now=NOW))
    assert p.state == NormalizedExecutionState.SUBMISSION_PENDING
    assert SubmissionLifecycle.ambiguous(p).state == NormalizedExecutionState.RECONCILIATION_REQUIRED
def test_partial_fill_cancel_releases_unused_capacity(tmp_path):
    store,r,_,_=setup(tmp_path); c=ExecutionTruthConsumer(store)
    partial=c.apply(truth(r,1,ExecutionTruth.PARTIAL_FILL,"50"))
    assert partial.state == ReservationState.PARTIALLY_CONSUMED
    assert store.active_notional("p") == Decimal("150")
    cancelled=c.apply(truth(r,2,ExecutionTruth.CANCELLED,"50"))
    assert cancelled.state == ReservationState.RELEASED
    assert cancelled.consumed_notional == Decimal("50")
    assert store.active_notional("p") == 0

def test_duplicate_truth_is_idempotent(tmp_path):
    store,r,_,_=setup(tmp_path); c=ExecutionTruthConsumer(store)
    t=truth(r,1,ExecutionTruth.PARTIAL_FILL,"50")
    first=c.apply(t); second=c.apply(t)
    assert first.consumed_notional == second.consumed_notional == Decimal("50")

def test_out_of_order_and_fill_regression_fail_closed(tmp_path):
    store,r,_,_=setup(tmp_path); c=ExecutionTruthConsumer(store)
    c.apply(truth(r,2,ExecutionTruth.PARTIAL_FILL,"100"))
    with pytest.raises(TruthConflict,match="TRUTH_SEQUENCE_REGRESSION"):
        c.apply(truth(r,1,ExecutionTruth.PARTIAL_FILL,"100"))
    with pytest.raises(TruthConflict,match="CUMULATIVE_FILL_REGRESSION"):
        c.apply(truth(r,3,ExecutionTruth.PARTIAL_FILL,"50"))
def test_unknown_retains_capacity_until_reconciliation(tmp_path):
    store,r,_,_=setup(tmp_path); c=ExecutionTruthConsumer(store)
    c.apply(truth(r,1,ExecutionTruth.UNKNOWN))
    assert store.active_notional("p") == Decimal("200")
    c.apply(truth(r,2,ExecutionTruth.REJECTED,"0",generation=1))
    assert store.active_notional("p") == 0

def test_terminal_regression_requires_new_generation(tmp_path):
    store,r,_,_=setup(tmp_path); c=ExecutionTruthConsumer(store)
    c.apply(truth(r,1,ExecutionTruth.FILLED,"200"))
    with pytest.raises(TruthConflict,match="TERMINAL_STATE_REGRESSION"):
        c.apply(truth(r,2,ExecutionTruth.PARTIAL_FILL,"200"))

def test_identity_mismatch_fails_closed(tmp_path):
    store,r,_,_=setup(tmp_path); c=ExecutionTruthConsumer(store)
    t=truth(r,1,ExecutionTruth.OPEN)
    bad=EQS07ExecutionTruthV2(t.reservation_id,t.execution_id,uuid4(),t.venue,
        t.instrument,t.execution_state,t.cumulative_filled_notional,t.truth_sequence,
        t.reconciliation_generation,t.observed_at,t.evidence_ref)
    with pytest.raises(TruthConflict,match="INTENT_ID_MISMATCH"):
        c.apply(bad)

def test_truth_cursor_survives_consumer_restart(tmp_path):
    store,r,_,_=setup(tmp_path)
    ExecutionTruthConsumer(store).apply(truth(r,2,ExecutionTruth.PARTIAL_FILL,"100"))
    restarted=ExecutionTruthConsumer(RiskReservationStore(store.path))
    with pytest.raises(TruthConflict,match="TRUTH_SEQUENCE_REGRESSION"):
        restarted.apply(truth(r,1,ExecutionTruth.PARTIAL_FILL,"100"))

def test_contradictory_duplicate_truth_fails_closed(tmp_path):
    store,r,_,_=setup(tmp_path); c=ExecutionTruthConsumer(store)
    c.apply(truth(r,1,ExecutionTruth.OPEN))
    with pytest.raises(TruthConflict,match="CONTRADICTORY_DUPLICATE_TRUTH"):
        c.apply(truth(r,1,ExecutionTruth.PARTIAL_FILL,"10"))

def test_live_authority_contract_cannot_be_constructed(tmp_path):
    store,r,o,_=setup(tmp_path)
    with pytest.raises(ValueError,match="LIVE capital authority is disabled"):
        CapitalExecutionAuthorityV1(
            r.reservation_id,r.decision_id,r.intent_id,"m",1,SID,"v","p",
            "SYNTH","XYZ",o.side,o.order_type,Decimal("2"),Decimal("2"),Decimal("100"),
            None,False,Decimal("200"),NOW,NOW+timedelta(minutes=1),"s","p","LIVE")

def test_fill_cannot_exceed_reserved_capital(tmp_path):
    store,r,_,_=setup(tmp_path); c=ExecutionTruthConsumer(store)
    with pytest.raises(TruthConflict,match="FILL_EXCEEDS_RESERVATION"):
        c.apply(truth(r,1,ExecutionTruth.FILLED,"201"))
