from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest
from quant_system.paper import PaperTradingEngine
from quant_system.shadow import ShadowExecutionEngine


def assumptions():
    return ExecutionAssumptions(Decimal("1"), Decimal("2"), Decimal("1"), Decimal("1"), Decimal("0"), Decimal("0"), 0)


def test_paper_engine_uses_simulator_and_ledger_end_to_end():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    engine = PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions()), initial_cash=Decimal("10000"))
    order = OrderRequest(uuid4(), "ABC", Side.BUY, Decimal("10"), OrderType.MARKET, start, Decimal("100"))
    engine.submit(order)
    fills = engine.on_bar(BarEvent("ABC", start + timedelta(minutes=1), Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1000")))
    assert len(fills) == 1
    snap = engine.snapshot({"ABC": Decimal("100")})
    assert snap.positions["ABC"] == Decimal("10")
    assert snap.cash < Decimal("9000")
    assert engine.ledger.reconcile().ok


def test_shadow_path_records_decision_without_venue_submission():
    adapter = InMemoryBrokerAdapter()
    shadow = ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False))
    order = OrderRequest(uuid4(), "ABC", Side.BUY, Decimal("1"), OrderType.MARKET, datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("100"))
    decision = shadow.evaluate_order(order)
    assert not decision.submission_result.sent
    assert decision.submission_result.reason == "VENUE_SUBMISSION_DISABLED"
    assert len(shadow.decisions) == 1
    assert adapter.submitted == []


def test_shadow_refuses_submission_enabled_gateway():
    try:
        ShadowExecutionEngine(BrokerGateway(InMemoryBrokerAdapter(), venue_submission_enabled=True))
    except ValueError as exc:
        assert "disabled" in str(exc)
    else:
        raise AssertionError("shadow path must fail closed")
