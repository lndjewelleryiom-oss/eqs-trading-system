from datetime import datetime, timezone
from decimal import Decimal
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest

from quant_system.risk.contracts import RiskDecisionV1, RiskDirection, RiskDisposition, ReservationState
from quant_system.risk.reservations import RiskReservationStore, ReservationConflict

NOW = datetime(2026,9,26,10,0,tzinfo=timezone.utc)
SID = uuid4()

def decision(intent=None):
    return RiskDecisionV1.create(
        intent_id=intent or uuid4(), strategy_id=SID, mandate_id="m1", mandate_version=1,
        input_state_ref="state", disposition=RiskDisposition.ALLOW,
        direction=RiskDirection.RISK_INCREASING, requested_quantity=Decimal("5"),
        authorised_quantity=Decimal("5"), reason_codes=("WITHIN_MANDATE",),
        limits_evaluated=("GROSS",), limits_consumed=("GROSS",), decided_at=NOW,
        risk_engine_version="test", policy_version="test")

def test_idempotent_reservation_survives_reopen(tmp_path):
    path=tmp_path/"risk.db"; d=decision()
    a=RiskReservationStore(path).reserve(d,portfolio_id="p",instrument="X",venue="S",
        reference_price=Decimal("100"),max_total_reserved=Decimal("1000"),now=NOW)
    b=RiskReservationStore(path).reserve(d,portfolio_id="p",instrument="X",venue="S",
        reference_price=Decimal("100"),max_total_reserved=Decimal("1000"),now=NOW)
    assert a.reservation_id == b.reservation_id
    assert RiskReservationStore(path).active_notional("p") == Decimal("500")
def test_unknown_execution_retains_capacity(tmp_path):
    store=RiskReservationStore(tmp_path/"risk.db")
    r=store.reserve(decision(),portfolio_id="p",instrument="X",venue="S",
        reference_price=Decimal("100"),max_total_reserved=Decimal("500"),now=NOW)
    store.transition(r.reservation_id,state=ReservationState.UNKNOWN_EXECUTION,now=NOW)
    assert store.active_notional("p") == Decimal("500")
    with pytest.raises(ReservationConflict):
        store.reserve(decision(),portfolio_id="p",instrument="X",venue="S",
            reference_price=Decimal("100"),max_total_reserved=Decimal("500"),now=NOW)

def test_release_restores_capacity(tmp_path):
    store=RiskReservationStore(tmp_path/"risk.db")
    r=store.reserve(decision(),portfolio_id="p",instrument="X",venue="S",
        reference_price=Decimal("100"),max_total_reserved=Decimal("500"),now=NOW)
    store.transition(r.reservation_id,state=ReservationState.RELEASED,now=NOW)
    assert store.active_notional("p") == 0

def test_concurrent_requests_cannot_double_spend(tmp_path):
    path=tmp_path/"risk.db"
    def attempt(_):
        try:
            RiskReservationStore(path).reserve(decision(),portfolio_id="p",instrument="X",
                venue="S",reference_price=Decimal("100"),max_total_reserved=Decimal("500"),now=NOW)
            return True
        except ReservationConflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(attempt, range(2)))
    assert sorted(results) == [False, True]
    assert RiskReservationStore(path).active_notional("p") == Decimal("500")
