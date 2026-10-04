from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Mapping

from quant_system.backtest.events import CashDividendEvent, SplitEvent
from quant_system.core.enums import Side


ZERO = Decimal("0")
BPS = Decimal("10000")
SECONDS_PER_YEAR = Decimal("31536000")  # 365 days; explicit deterministic convention for F2.


class JournalType(StrEnum):
    FILL = "FILL"
    FINANCING = "FINANCING"
    BORROW = "BORROW"
    SPLIT = "SPLIT"
    DIVIDEND = "DIVIDEND"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    sequence: int
    timestamp: datetime
    entry_type: JournalType
    symbol: str | None
    cash_delta: Decimal
    quantity_delta: Decimal = ZERO
    detail: str = ""


@dataclass(slots=True)
class PositionState:
    quantity: Decimal = ZERO
    average_cost: Decimal = ZERO
    realized_pnl: Decimal = ZERO


@dataclass(frozen=True, slots=True)
class LedgerPositionSnapshot:
    quantity: Decimal
    average_cost: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class LedgerState:
    initial_cash: Decimal
    cash: Decimal
    positions: Mapping[str, LedgerPositionSnapshot]
    commissions: Decimal
    financing_costs: Decimal
    borrow_costs: Decimal
    dividend_cashflow: Decimal
    journal: tuple[JournalEntry, ...]
    sequence: int


@dataclass(frozen=True, slots=True)
class AccountingSnapshot:
    cash: Decimal
    positions: Mapping[str, Decimal]
    average_costs: Mapping[str, Decimal]
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    commissions: Decimal
    financing_costs: Decimal
    borrow_costs: Decimal
    dividend_cashflow: Decimal
    market_value: Decimal
    gross_notional: Decimal
    net_notional: Decimal
    equity: Decimal


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    ok: bool
    expected_cash: Decimal
    actual_cash: Decimal
    expected_positions: Mapping[str, Decimal]
    actual_positions: Mapping[str, Decimal]
    cash_difference: Decimal
    position_differences: Mapping[str, Decimal]


class Ledger:
    """Journal-backed double-entry-like trading ledger for deterministic backtests.

    Cash and signed position quantities are independently reconstructable from the
    append-only journal. Cost-basis/P&L state is maintained for reporting, while
    reconciliation intentionally recomputes cash and quantities from primitive
    journal deltas rather than trusting current aggregate balances.
    """

    def __init__(self, initial_cash: Decimal) -> None:
        self.initial_cash = Decimal(initial_cash)
        self.cash = Decimal(initial_cash)
        self._positions: dict[str, PositionState] = {}
        self.commissions = ZERO
        self.financing_costs = ZERO
        self.borrow_costs = ZERO
        self.dividend_cashflow = ZERO
        self._journal: list[JournalEntry] = []
        self._sequence = 0

    @classmethod
    def with_cash(cls, cash: Decimal) -> "Ledger":
        return cls(cash)

    @property
    def positions(self) -> dict[str, Decimal]:
        return {symbol: state.quantity for symbol, state in self._positions.items() if state.quantity != ZERO}

    @property
    def journal(self) -> tuple[JournalEntry, ...]:
        return tuple(self._journal)

    def export_state(self) -> LedgerState:
        return LedgerState(
            initial_cash=self.initial_cash,
            cash=self.cash,
            positions={
                symbol: LedgerPositionSnapshot(state.quantity, state.average_cost, state.realized_pnl)
                for symbol, state in self._positions.items()
            },
            commissions=self.commissions,
            financing_costs=self.financing_costs,
            borrow_costs=self.borrow_costs,
            dividend_cashflow=self.dividend_cashflow,
            journal=tuple(self._journal),
            sequence=self._sequence,
        )

    @classmethod
    def from_state(cls, state: LedgerState) -> "Ledger":
        ledger = cls(state.initial_cash)
        ledger.cash = Decimal(state.cash)
        ledger._positions = {
            symbol: PositionState(Decimal(position.quantity), Decimal(position.average_cost), Decimal(position.realized_pnl))
            for symbol, position in state.positions.items()
        }
        ledger.commissions = Decimal(state.commissions)
        ledger.financing_costs = Decimal(state.financing_costs)
        ledger.borrow_costs = Decimal(state.borrow_costs)
        ledger.dividend_cashflow = Decimal(state.dividend_cashflow)
        ledger._journal = list(state.journal)
        ledger._sequence = int(state.sequence)
        if ledger._sequence != len(ledger._journal):
            raise ValueError("ledger state sequence does not match journal length")
        reconciliation = ledger.reconcile()
        if not reconciliation.ok:
            raise ValueError("ledger state fails journal reconciliation")
        return ledger

    def position_state(self, symbol: str) -> PositionState:
        state = self._positions.get(symbol)
        if state is None:
            return PositionState()
        return PositionState(state.quantity, state.average_cost, state.realized_pnl)

    def _append(
        self,
        *,
        timestamp: datetime,
        entry_type: JournalType,
        symbol: str | None,
        cash_delta: Decimal,
        quantity_delta: Decimal = ZERO,
        detail: str = "",
    ) -> None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("journal timestamps must be timezone-aware")
        self._sequence += 1
        self._journal.append(
            JournalEntry(
                sequence=self._sequence,
                timestamp=timestamp,
                entry_type=entry_type,
                symbol=symbol,
                cash_delta=cash_delta,
                quantity_delta=quantity_delta,
                detail=detail,
            )
        )

    def apply(self, fill: object, side: Side) -> None:
        """Apply a simulated fill and update cash, cost basis and realized P&L."""
        quantity = Decimal(fill.quantity)
        price = Decimal(fill.price)
        commission = Decimal(fill.commission)
        if quantity <= ZERO or price <= ZERO or commission < ZERO:
            raise ValueError("invalid fill values")

        signed_qty = quantity if side == Side.BUY else -quantity
        gross_cash_delta = -(signed_qty * price)
        cash_delta = gross_cash_delta - commission
        self.cash += cash_delta
        self.commissions += commission

        symbol = str(fill.symbol)
        state = self._positions.setdefault(symbol, PositionState())
        old_qty = state.quantity
        old_avg = state.average_cost
        new_qty = old_qty + signed_qty

        if old_qty == ZERO or (old_qty > ZERO and signed_qty > ZERO) or (old_qty < ZERO and signed_qty < ZERO):
            old_abs = abs(old_qty)
            add_abs = abs(signed_qty)
            total_abs = old_abs + add_abs
            state.average_cost = ((old_abs * old_avg) + (add_abs * price)) / total_abs
        else:
            closing_qty = min(abs(old_qty), abs(signed_qty))
            direction = Decimal("1") if old_qty > ZERO else Decimal("-1")
            state.realized_pnl += closing_qty * (price - old_avg) * direction
            if new_qty == ZERO:
                state.average_cost = ZERO
            elif (new_qty > ZERO) != (old_qty > ZERO):
                # The trade crossed through flat; residual exposure starts at this fill price.
                state.average_cost = price

        state.quantity = new_qty
        self._append(
            timestamp=fill.timestamp,
            entry_type=JournalType.FILL,
            symbol=symbol,
            cash_delta=cash_delta,
            quantity_delta=signed_qty,
            detail=f"price={price};commission={commission};side={side}",
        )

    def accrue_costs(
        self,
        *,
        start: datetime,
        end: datetime,
        marks: Mapping[str, Decimal],
        financing_bps_annual: Decimal,
        borrow_bps_annual: Decimal,
    ) -> tuple[Decimal, Decimal]:
        """Accrue debit-cash financing and short-stock borrow using ACT/365.

        Positive cash receives no interest in F2, which is deliberately conservative.
        Borrow is charged on marked short notional. Missing marks for shorts fail closed.
        """
        if start.tzinfo is None or start.utcoffset() is None or end.tzinfo is None or end.utcoffset() is None:
            raise ValueError("accrual timestamps must be timezone-aware")
        if end < start:
            raise ValueError("end must be >= start")
        if financing_bps_annual < ZERO or borrow_bps_annual < ZERO:
            raise ValueError("annual cost rates must be non-negative")

        elapsed_seconds = Decimal(str((end - start).total_seconds()))
        year_fraction = elapsed_seconds / SECONDS_PER_YEAR

        financing = ZERO
        if self.cash < ZERO and financing_bps_annual > ZERO and year_fraction > ZERO:
            financing = abs(self.cash) * financing_bps_annual / BPS * year_fraction
            self.cash -= financing
            self.financing_costs += financing
            self._append(
                timestamp=end,
                entry_type=JournalType.FINANCING,
                symbol=None,
                cash_delta=-financing,
                detail=f"start={start.isoformat()};annual_bps={financing_bps_annual}",
            )

        borrow = ZERO
        if borrow_bps_annual > ZERO and year_fraction > ZERO:
            short_notional = ZERO
            for symbol, state in self._positions.items():
                if state.quantity >= ZERO:
                    continue
                if symbol not in marks:
                    raise ValueError(f"missing mark for short position {symbol}")
                mark = Decimal(marks[symbol])
                if mark <= ZERO:
                    raise ValueError("marks must be positive")
                short_notional += abs(state.quantity) * mark
            borrow = short_notional * borrow_bps_annual / BPS * year_fraction
            if borrow > ZERO:
                self.cash -= borrow
                self.borrow_costs += borrow
                self._append(
                    timestamp=end,
                    entry_type=JournalType.BORROW,
                    symbol=None,
                    cash_delta=-borrow,
                    detail=f"start={start.isoformat()};annual_bps={borrow_bps_annual}",
                )
        return financing, borrow

    def apply_split(self, event: SplitEvent) -> None:
        state = self._positions.get(event.symbol)
        if state is None or state.quantity == ZERO:
            return
        old_qty = state.quantity
        new_qty = old_qty * event.ratio
        quantity_delta = new_qty - old_qty
        state.quantity = new_qty
        state.average_cost = state.average_cost / event.ratio
        self._append(
            timestamp=event.timestamp,
            entry_type=JournalType.SPLIT,
            symbol=event.symbol,
            cash_delta=ZERO,
            quantity_delta=quantity_delta,
            detail=f"ratio={event.ratio}",
        )

    def apply_dividend(self, event: CashDividendEvent) -> Decimal:
        state = self._positions.get(event.symbol)
        if state is None or state.quantity == ZERO:
            return ZERO
        cashflow = state.quantity * event.amount_per_share
        self.cash += cashflow
        self.dividend_cashflow += cashflow
        self._append(
            timestamp=event.timestamp,
            entry_type=JournalType.DIVIDEND,
            symbol=event.symbol,
            cash_delta=cashflow,
            detail=f"amount_per_share={event.amount_per_share}",
        )
        return cashflow

    def snapshot(self, marks: Mapping[str, Decimal]) -> AccountingSnapshot:
        market_value = ZERO
        gross_notional = ZERO
        unrealized = ZERO
        average_costs: dict[str, Decimal] = {}
        realized = ZERO

        for symbol, state in self._positions.items():
            realized += state.realized_pnl
            if state.quantity == ZERO:
                continue
            if symbol not in marks:
                raise ValueError(f"missing mark for open position {symbol}")
            mark = Decimal(marks[symbol])
            if mark <= ZERO:
                raise ValueError("marks must be positive")
            value = state.quantity * mark
            market_value += value
            gross_notional += abs(value)
            unrealized += state.quantity * (mark - state.average_cost)
            average_costs[symbol] = state.average_cost

        positions = self.positions
        return AccountingSnapshot(
            cash=self.cash,
            positions=positions,
            average_costs=average_costs,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            commissions=self.commissions,
            financing_costs=self.financing_costs,
            borrow_costs=self.borrow_costs,
            dividend_cashflow=self.dividend_cashflow,
            market_value=market_value,
            gross_notional=gross_notional,
            net_notional=market_value,
            equity=self.cash + market_value,
        )

    def reconcile(self) -> ReconciliationResult:
        expected_cash = self.initial_cash
        expected_positions: dict[str, Decimal] = {}
        last_sequence = 0
        for entry in self._journal:
            if entry.sequence != last_sequence + 1:
                raise ValueError("journal sequence is not contiguous")
            last_sequence = entry.sequence
            expected_cash += entry.cash_delta
            if entry.symbol is not None and entry.quantity_delta != ZERO:
                expected_positions[entry.symbol] = expected_positions.get(entry.symbol, ZERO) + entry.quantity_delta

        expected_positions = {k: v for k, v in expected_positions.items() if v != ZERO}
        actual_positions = self.positions
        symbols = sorted(set(expected_positions) | set(actual_positions))
        position_differences = {
            symbol: actual_positions.get(symbol, ZERO) - expected_positions.get(symbol, ZERO)
            for symbol in symbols
            if actual_positions.get(symbol, ZERO) != expected_positions.get(symbol, ZERO)
        }
        cash_difference = self.cash - expected_cash
        return ReconciliationResult(
            ok=(cash_difference == ZERO and not position_differences),
            expected_cash=expected_cash,
            actual_cash=self.cash,
            expected_positions=expected_positions,
            actual_positions=actual_positions,
            cash_difference=cash_difference,
            position_differences=position_differences,
        )
