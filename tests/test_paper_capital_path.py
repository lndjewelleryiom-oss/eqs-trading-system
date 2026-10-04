from datetime import timedelta
from decimal import Decimal

from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.paper.engine import PaperTradingEngine
from quant_system.paper.capital_path import PaperCapitalExecutionPath
from quant_system.risk.control import CapitalControlService
from quant_system.risk.execution_bridge import ExecutionTruthConsumer
from quant_system.risk.nonlive_bridge import NonLiveCapitalBridge
from quant_system.risk.reservations import RiskReservationStore
from quant_system.risk.contracts import ReservationState
from test_pretrade_authority import NOW,mandate,state,order

def assumptions():
    return ExecutionAssumptions(Decimal("10"),Decimal("20"),Decimal("10"),Decimal("0"),
        Decimal("0"),Decimal("0"),0,Decimal("1"))

def bar(minute):
    return BarEvent("XYZ",NOW+timedelta(minutes=minute),Decimal("100"),Decimal("101"),
        Decimal("99"),Decimal("100"),Decimal("100"))

def setup_path(tmp_path):
    store=RiskReservationStore(tmp_path/"paper-capital.db")
    auth=NonLiveCapitalBridge(CapitalControlService(store)).authorise(
        order("2"),mandate(),state(),strategy_version="fixture",execution_mode="PAPER")
    engine=PaperTradingEngine(ConservativeBarExecutionSimulator(
        assumptions(),max_volume_participation=Decimal("0.01")),initial_cash=Decimal("10000"))
    path=PaperCapitalExecutionPath(engine,ExecutionTruthConsumer(store))
    return store,auth,engine,path
def test_end_to_end_partial_then_full_settlement(tmp_path):
    store,auth,engine,path=setup_path(tmp_path); a=auth.execution_authority
    assert a is not None
    base=order("2"); o=type(base)(base.strategy_id,base.symbol,base.side,base.quantity,
        base.order_type,base.decision_time,base.reference_price,base.limit_price,base.reduce_only,a.intent_id)
    path.submit(o,a)
    first=path.on_bar(bar(1),a)
    assert len(first)==1 and first[0].fill.quantity==Decimal("1.0")
    r=store.get(a.reservation_id)
    assert r.state==ReservationState.PARTIALLY_CONSUMED
    assert r.consumed_notional==Decimal("100.0")
    for minute in range(2,30):
        path.on_bar(bar(minute),a)
        if store.get(a.reservation_id).state==ReservationState.CONSUMED:
            break
    r=store.get(a.reservation_id)
    assert r.state==ReservationState.CONSUMED
    assert r.consumed_notional==r.reserved_notional==Decimal("200")

def test_crash_after_reservation_before_submission_preserves_capacity(tmp_path):
    store,auth,_,_=setup_path(tmp_path); a=auth.execution_authority
    reopened=RiskReservationStore(store.path)
    assert reopened.get(a.reservation_id).state==ReservationState.RESERVED
    assert reopened.active_notional("PAPER-1")==Decimal("200")

def test_restart_after_partial_fill_continues_monotonic_truth(tmp_path):
    store,auth,engine,path=setup_path(tmp_path); a=auth.execution_authority
    base=order("2"); o=type(base)(base.strategy_id,base.symbol,base.side,base.quantity,
        base.order_type,base.decision_time,base.reference_price,base.limit_price,base.reduce_only,a.intent_id)
    path.submit(o,a)
    path.on_bar(bar(1),a)
    saved_engine=engine.export_state()
    new_engine=PaperTradingEngine(ConservativeBarExecutionSimulator(
        assumptions(),max_volume_participation=Decimal("0.01")),initial_cash=Decimal("10000"))
    new_engine.restore_state(saved_engine)
    reopened=RiskReservationStore(store.path)
    recovered=PaperCapitalExecutionPath(new_engine,ExecutionTruthConsumer(reopened))
    recovered.recover_from_truth_cursor(a)
    for minute in range(2,30):
        recovered.on_bar(bar(minute),a)
        if reopened.get(a.reservation_id).state==ReservationState.CONSUMED:
            break
    r=reopened.get(a.reservation_id)
    assert r.state==ReservationState.CONSUMED
    assert r.consumed_notional==Decimal("200")
