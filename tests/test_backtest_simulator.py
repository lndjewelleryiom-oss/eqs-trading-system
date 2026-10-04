from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator, Ledger
from quant_system.core.enums import OrderType, Side
from quant_system.execution.models import OrderRequest


def assumptions(**overrides):
    base = dict(
        commission_bps=Decimal("10"), spread_bps=Decimal("20"),
        slippage_bps=Decimal("10"), impact_bps=Decimal("0"),
        financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("0"),
        latency_ms=0, partial_fill_fraction=Decimal("1"),
    )
    base.update(overrides)
    return ExecutionAssumptions(**base)


def make_order(
    t,
    *,
    quantity="2",
    side=Side.BUY,
    order_type=OrderType.MARKET,
    limit_price=None,
):
    if order_type == OrderType.LIMIT and limit_price is None:
        limit_price = "99"
    return OrderRequest(
        strategy_id=uuid4(), symbol="XYZ", side=side, quantity=Decimal(quantity),
        order_type=order_type, decision_time=t, reference_price=Decimal("100"),
        limit_price=Decimal(limit_price) if limit_price is not None else None,
    )


def bar(t, *, open_="100", high="101", low="99", close="100", volume="1000"):
    return BarEvent(
        "XYZ", t, Decimal(open_), Decimal(high), Decimal(low), Decimal(close), Decimal(volume)
    )


def test_market_order_never_fills_same_bar():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(assumptions())
    sim.submit(make_order(t))
    assert sim.on_bar(bar(t)) == ()
    assert len(sim.on_bar(bar(t + timedelta(minutes=1)))) == 1


def test_costs_are_applied_adversely_and_ledger_is_exact():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(assumptions())
    order = make_order(t, quantity="2")
    sim.submit(order)
    [(filled_order, fill)] = sim.on_bar(bar(t + timedelta(minutes=1)))
    # half-spread 10 bps + slippage 10 bps = 20 bps adverse: 100 * 1.002 = 100.2
    assert fill.price == Decimal("100.200")
    assert fill.commission == Decimal("0.200400")
    ledger = Ledger.with_cash(Decimal("1000"))
    ledger.apply(fill, filled_order.side)
    assert ledger.cash == Decimal("799.399600")
    assert ledger.positions["XYZ"] == Decimal("2")


def test_volume_participation_creates_partial_fill():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(assumptions(), max_volume_participation=Decimal("0.05"))
    sim.submit(make_order(t, quantity="10"))
    [(order, fill)] = sim.on_bar(bar(t + timedelta(minutes=1), volume="100"))
    assert fill.quantity == Decimal("5.00")
    assert sim.pending_count == 1


def test_duplicate_order_submission_is_rejected():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(assumptions())
    order = make_order(t)
    sim.submit(order)
    with pytest.raises(ValueError, match="duplicate"):
        sim.submit(order)


def test_buy_limit_requires_touch_and_fills_at_limit_not_optimistic_low():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(assumptions())
    sim.submit(make_order(t, quantity="2", order_type=OrderType.LIMIT, limit_price="99"))
    assert sim.on_bar(bar(t + timedelta(minutes=1), low="99.5")) == ()
    [(order, fill)] = sim.on_bar(bar(t + timedelta(minutes=2), low="98", close="99.5"))
    assert order.side == Side.BUY
    assert fill.price == Decimal("99")
    assert fill.spread_bps_applied == Decimal("0")
    assert fill.slippage_bps_applied == Decimal("0")
    assert fill.impact_bps_applied == Decimal("0")


def test_sell_limit_requires_high_touch_and_fills_at_limit():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(assumptions())
    sim.submit(make_order(t, side=Side.SELL, order_type=OrderType.LIMIT, limit_price="102"))
    assert sim.on_bar(bar(t + timedelta(minutes=1), high="101.5")) == ()
    [(order, fill)] = sim.on_bar(bar(t + timedelta(minutes=2), high="103", close="102"))
    assert order.side == Side.SELL
    assert fill.price == Decimal("102")


def test_latency_blocks_fill_until_first_eligible_bar():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(assumptions(latency_ms=90_000))
    sim.submit(make_order(t))
    assert sim.on_bar(bar(t + timedelta(minutes=1))) == ()
    assert len(sim.on_bar(bar(t + timedelta(minutes=2)))) == 1


def test_market_impact_scales_adversely_with_participation():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(
        assumptions(commission_bps=Decimal("0"), spread_bps=Decimal("0"), slippage_bps=Decimal("0"), impact_bps=Decimal("100")),
        max_volume_participation=Decimal("0.10"),
    )
    sim.submit(make_order(t, quantity="10"))
    [(_, fill)] = sim.on_bar(bar(t + timedelta(minutes=1), volume="100"))
    assert fill.quantity == Decimal("10")
    assert fill.impact_bps_applied == Decimal("100")
    assert fill.price == Decimal("101.00")


def test_zero_volume_cannot_fill_and_out_of_order_bars_are_rejected():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    sim = ConservativeBarExecutionSimulator(assumptions())
    sim.submit(make_order(t))
    assert sim.on_bar(bar(t + timedelta(minutes=2), volume="0")) == ()
    with pytest.raises(ValueError, match="strictly increasing"):
        sim.on_bar(bar(t + timedelta(minutes=1)))


def test_negative_execution_cost_assumptions_fail_closed():
    with pytest.raises(ValueError, match="impact_bps"):
        assumptions(impact_bps=Decimal("-1"))
