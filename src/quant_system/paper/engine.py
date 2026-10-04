from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_system.backtest.accounting import AccountingSnapshot, Ledger, LedgerState
from quant_system.backtest.events import BarEvent
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator, SimulatedFill, SimulatorState
from quant_system.execution.models import OrderRequest


@dataclass(frozen=True, slots=True)
class PaperFillRecord:
    order: OrderRequest
    fill: SimulatedFill


@dataclass(frozen=True, slots=True)
class PaperEngineState:
    ledger: LedgerState
    simulator: SimulatorState
    fills: tuple[PaperFillRecord, ...]


class PaperTradingEngine:
    """Uses the production simulation/accounting primitives without venue submission."""

    def __init__(self, simulator: ConservativeBarExecutionSimulator, *, initial_cash: Decimal):
        self.simulator = simulator
        self.ledger = Ledger.with_cash(initial_cash)
        self._fills: list[PaperFillRecord] = []

    def submit(self, order: OrderRequest) -> None:
        self.simulator.submit(order)

    def on_bar(self, bar: BarEvent) -> tuple[PaperFillRecord, ...]:
        records: list[PaperFillRecord] = []
        for order, fill in self.simulator.on_bar(bar):
            self.ledger.apply(fill, order.side)
            record = PaperFillRecord(order, fill)
            self._fills.append(record)
            records.append(record)
        return tuple(records)

    @property
    def fills(self) -> tuple[PaperFillRecord, ...]:
        return tuple(self._fills)

    def export_state(self) -> PaperEngineState:
        return PaperEngineState(self.ledger.export_state(), self.simulator.export_state(), tuple(self._fills))

    def restore_state(self, state: PaperEngineState) -> None:
        self.ledger = Ledger.from_state(state.ledger)
        self.simulator.restore_state(state.simulator)
        self._fills = list(state.fills)

    def snapshot(self, marks: dict[str, Decimal]) -> AccountingSnapshot:
        return self.ledger.snapshot(marks)
