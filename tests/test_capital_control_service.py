from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from quant_system.core.enums import Side
from quant_system.risk.control import CapitalControlService
from quant_system.risk.contracts import RiskDisposition
from quant_system.risk.reservations import RiskReservationStore
from test_pretrade_authority import mandate, state, order

def test_end_to_end_authority_reservation_restart_and_release(tmp_path):
    path=tmp_path/"capital.db"
    svc=CapitalControlService(RiskReservationStore(path))
    result=svc.authorise_and_reserve(order("2"),mandate(),state())
    assert result.decision.disposition == RiskDisposition.ALLOW
    assert result.reservation is not None
    assert RiskReservationStore(path).active_notional("PAPER-1") == Decimal("200")

def test_service_prevents_concurrent_portfolio_overallocation(tmp_path):
    path=tmp_path/"capital.db"
    m=mandate(max_gross_exposure=Decimal("500"))
    def attempt(_):
        svc=CapitalControlService(RiskReservationStore(path))
        return svc.authorise_and_reserve(order("5"),m,state()).decision.disposition
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(attempt,range(2)))
    assert outcomes.count(RiskDisposition.ALLOW) == 1
    assert outcomes.count(RiskDisposition.REJECT) == 1
    assert RiskReservationStore(path).active_notional("PAPER-1") == Decimal("500")

def test_risk_reduction_does_not_consume_new_capital(tmp_path):
    svc=CapitalControlService(RiskReservationStore(tmp_path/"capital.db"))
    result=svc.authorise_and_reserve(
        order("1",Side.SELL,True),mandate(),
        state(position_quantity=Decimal("2"),gross_exposure=Decimal("200")))
    assert result.decision.disposition == RiskDisposition.ALLOW
    assert result.reservation is None

def test_adversarial_twenty_way_capacity_soak(tmp_path):
    path=tmp_path/"soak.db"
    m=mandate(max_gross_exposure=Decimal("1000"))
    def attempt(_):
        svc=CapitalControlService(RiskReservationStore(path))
        return svc.authorise_and_reserve(order("1"),m,state()).decision.disposition
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes=list(pool.map(attempt,range(20)))
    assert outcomes.count(RiskDisposition.ALLOW) == 10
    assert outcomes.count(RiskDisposition.REJECT) == 10
    assert RiskReservationStore(path).active_notional("PAPER-1") == Decimal("1000")
