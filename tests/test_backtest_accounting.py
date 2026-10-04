from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.backtest.accounting import Ledger
from quant_system.backtest.events import CashDividendEvent, SplitEvent
from quant_system.backtest.simulator import SimulatedFill
from quant_system.core.enums import Side


def fill(t, *, symbol="XYZ", qty="1", price="100", commission="0"):
    return SimulatedFill(
        order_id=uuid4(), symbol=symbol, quantity=Decimal(qty), price=Decimal(price),
        commission=Decimal(commission), timestamp=t,
    )


def test_round_trip_accounting_tracks_realized_unrealized_fees_and_equity():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger.with_cash(Decimal("1000"))
    ledger.apply(fill(t, qty="2", price="100", commission="1"), Side.BUY)
    ledger.apply(fill(t + timedelta(minutes=1), qty="1", price="110", commission="1.1"), Side.SELL)

    state = ledger.position_state("XYZ")
    snapshot = ledger.snapshot({"XYZ": Decimal("105")})
    assert state.quantity == Decimal("1")
    assert state.average_cost == Decimal("100")
    assert state.realized_pnl == Decimal("10")
    assert snapshot.realized_pnl == Decimal("10")
    assert snapshot.unrealized_pnl == Decimal("5")
    assert snapshot.commissions == Decimal("2.1")
    assert snapshot.cash == Decimal("907.9")
    assert snapshot.market_value == Decimal("105")
    assert snapshot.gross_notional == Decimal("105")
    assert snapshot.equity == Decimal("1012.9")


def test_crossing_through_flat_resets_cost_basis_for_new_short():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger.with_cash(Decimal("1000"))
    ledger.apply(fill(t, qty="1", price="100"), Side.BUY)
    ledger.apply(fill(t + timedelta(minutes=1), qty="2", price="110"), Side.SELL)
    state = ledger.position_state("XYZ")
    assert state.quantity == Decimal("-1")
    assert state.average_cost == Decimal("110")
    assert state.realized_pnl == Decimal("10")


def test_financing_cost_accrues_on_negative_cash_using_act_365():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger.with_cash(Decimal("0"))
    ledger.apply(fill(t, qty="1", price="100"), Side.BUY)
    financing, borrow = ledger.accrue_costs(
        start=t, end=t + timedelta(days=1), marks={"XYZ": Decimal("100")},
        financing_bps_annual=Decimal("36500"), borrow_bps_annual=Decimal("0"),
    )
    assert financing == Decimal("1")
    assert borrow == Decimal("0")
    assert ledger.cash == Decimal("-101")
    assert ledger.financing_costs == Decimal("1")


def test_borrow_cost_accrues_on_marked_short_notional():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger.with_cash(Decimal("1000"))
    ledger.apply(fill(t, qty="2", price="100"), Side.SELL)
    financing, borrow = ledger.accrue_costs(
        start=t, end=t + timedelta(days=1), marks={"XYZ": Decimal("120")},
        financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("36500"),
    )
    assert financing == Decimal("0")
    assert borrow == Decimal("2.4")
    assert ledger.borrow_costs == Decimal("2.4")
    assert ledger.cash == Decimal("1197.6")


def test_split_preserves_economic_cost_basis_and_dividend_hits_cash():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger.with_cash(Decimal("1000"))
    ledger.apply(fill(t, qty="2", price="100"), Side.BUY)
    ledger.apply_split(SplitEvent("XYZ", t + timedelta(days=1), Decimal("2")))
    state = ledger.position_state("XYZ")
    assert state.quantity == Decimal("4")
    assert state.average_cost == Decimal("50")
    cashflow = ledger.apply_dividend(CashDividendEvent("XYZ", t + timedelta(days=2), Decimal("1.5")))
    assert cashflow == Decimal("6.0")
    assert ledger.cash == Decimal("806.0")
    assert ledger.dividend_cashflow == Decimal("6.0")
    assert ledger.snapshot({"XYZ": Decimal("50")}).equity == Decimal("1006.0")


def test_short_pays_dividend_instead_of_receiving_it():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger.with_cash(Decimal("1000"))
    ledger.apply(fill(t, qty="3", price="100"), Side.SELL)
    cashflow = ledger.apply_dividend(CashDividendEvent("XYZ", t + timedelta(days=1), Decimal("2")))
    assert cashflow == Decimal("-6")
    assert ledger.cash == Decimal("1294")


def test_reconciliation_reconstructs_cash_and_positions_from_journal_and_detects_drift():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger.with_cash(Decimal("1000"))
    ledger.apply(fill(t, qty="2", price="100", commission="0.5"), Side.BUY)
    ledger.apply_split(SplitEvent("XYZ", t + timedelta(days=1), Decimal("2")))
    ledger.apply_dividend(CashDividendEvent("XYZ", t + timedelta(days=2), Decimal("1")))
    ledger.accrue_costs(
        start=t + timedelta(days=2), end=t + timedelta(days=3), marks={"XYZ": Decimal("50")},
        financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("0"),
    )
    result = ledger.reconcile()
    assert result.ok
    assert result.expected_cash == ledger.cash
    assert result.expected_positions == {"XYZ": Decimal("4")}

    ledger.cash += Decimal("0.01")  # simulate aggregate-state corruption
    drift = ledger.reconcile()
    assert not drift.ok
    assert drift.cash_difference == Decimal("0.01")

import pytest


def test_borrow_accrual_fails_closed_without_short_mark():
    t = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
    ledger = Ledger.with_cash(Decimal("1000"))
    ledger.apply(fill(t, qty="1", price="100"), Side.SELL)
    with pytest.raises(ValueError, match="missing mark"):
        ledger.accrue_costs(
            start=t, end=t + timedelta(days=1), marks={},
            financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("100"),
        )
