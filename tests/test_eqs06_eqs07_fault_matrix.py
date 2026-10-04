from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import pytest
from quant_system.risk.execution_bridge import ExecutionTruthConsumer, TruthConflict
from quant_system.risk.interfaces import ExecutionTruth
from quant_system.risk.reservations import RiskReservationStore, ReservationConflict
from test_eqs06_eqs07_bridge import setup, truth
from test_risk_reservations import decision
from test_pretrade_authority import NOW

def test_repeated_concurrent_reservation_fault_matrix(tmp_path):
    for run in range(10):
        path=tmp_path/f"race-{run}.db"
        def attempt(_):
            try:
                RiskReservationStore(path).reserve(
                    decision(),portfolio_id="p",instrument="X",venue="S",
                    reference_price=Decimal("100"),max_total_reserved=Decimal("500"),now=NOW)
                return 1
            except ReservationConflict:
                return 0
        with ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(attempt,range(4)))
        assert sum(results) == 1
        assert RiskReservationStore(path).active_notional("p") == Decimal("500")

def test_unknown_restart_then_authoritative_resolution(tmp_path):
    store,r,_,_=setup(tmp_path)
    ExecutionTruthConsumer(store).apply(truth(r,1,ExecutionTruth.UNKNOWN))
    reopened=RiskReservationStore(store.path)
    assert reopened.active_notional("p") == Decimal("200")
    ExecutionTruthConsumer(reopened).apply(truth(r,2,ExecutionTruth.CANCELLED,"0",1))
    assert reopened.active_notional("p") == 0
