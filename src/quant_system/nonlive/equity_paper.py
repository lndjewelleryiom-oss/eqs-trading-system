from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from uuid import UUID, uuid5

from quant_system.backtest.events import BarEvent
from quant_system.core.enums import OrderType, RiskAction, Side
from quant_system.execution.models import OrderRequest
from quant_system.paper.engine import PaperFillRecord, PaperTradingEngine
from quant_system.risk.policy import PortfolioRiskSnapshot, RiskDecision, RiskEngine
from quant_system.risk.shared03 import (
    Shared03Context,
    Shared03GateDisposition,
    Shared03NonLiveGate,
)
from quant_system.data.equities.models import EquityBarEvent

_ORDER_NAMESPACE = UUID("fcf29618-bda4-59c7-84ba-8ee013c2411f")


@dataclass(frozen=True, slots=True)
class EquityPaperSubmissionResult:
    order: OrderRequest
    action: RiskAction
    reason_codes: tuple[str, ...]
    submitted_to_paper_engine: bool


class EquityPaperAdapter:
    """Stocks/ETFs PAPER-only adapter.

    The adapter has no broker/gateway dependency. It reuses the authoritative RiskEngine,
    SHARED-03 non-live gate, and conservative bar simulator. A permitted order is submitted
    only to PaperTradingEngine and can fill only on a later eligible bar.
    """

    def __init__(
        self,
        *,
        paper_engine: PaperTradingEngine,
        risk_engine: RiskEngine,
        shared03_gate: Shared03NonLiveGate | None = None,
    ) -> None:
        self.paper_engine = paper_engine
        self.risk_engine = risk_engine
        self.shared03_gate = shared03_gate or Shared03NonLiveGate()

    @staticmethod
    def order_from_closed_bar(
        bar: EquityBarEvent,
        *,
        strategy_id: UUID,
        side: Side,
        quantity: Decimal,
    ) -> OrderRequest:
        bar.meta.assert_usable_at(bar.meta.available_at)
        payload = f"{strategy_id}|{bar.meta.instrument_id}|{side.value}|{quantity}|{bar.meta.canonical_identity()}"
        return OrderRequest(
            strategy_id=strategy_id,
            symbol=bar.meta.instrument_id,
            side=side,
            quantity=quantity,
            order_type=OrderType.MARKET,
            decision_time=bar.meta.available_at,
            reference_price=bar.close,
            order_id=uuid5(_ORDER_NAMESPACE, payload),
        )

    def evaluate_and_submit(
        self,
        bar: EquityBarEvent,
        order: OrderRequest,
        *,
        risk_snapshot: PortfolioRiskSnapshot,
        shared03_context: Shared03Context | None,
    ) -> EquityPaperSubmissionResult:
        if order.symbol != bar.meta.instrument_id:
            raise ValueError("order symbol must match equity bar instrument_id")
        if order.reference_price != bar.close:
            raise ValueError("order reference_price must equal source bar close")
        if order.decision_time < bar.meta.available_at:
            raise ValueError("order decision_time precedes source bar availability")

        effective_snapshot = replace(
            risk_snapshot,
            market_data_received_at=bar.meta.available_at,
        )
        raw = self.risk_engine.evaluate(order, effective_snapshot)
        action = raw.action
        reasons = raw.reason_codes

        if raw.action == RiskAction.ALLOW:
            if shared03_context is None:
                action = RiskAction.HALT
                reasons = tuple(sorted(set(reasons + ("SHARED03_CONTEXT_MISSING",))))
            elif shared03_context.execution_mode != "PAPER":
                action = RiskAction.HALT
                reasons = tuple(sorted(set(reasons + ("SHARED03_EXECUTION_MODE_MISMATCH",))))
            else:
                gate = self.shared03_gate.evaluate(order, shared03_context)
                if gate.disposition == Shared03GateDisposition.HALT:
                    action = RiskAction.HALT
                elif gate.disposition == Shared03GateDisposition.BLOCK:
                    action = RiskAction.BLOCK
                reasons = tuple(sorted(set(reasons + gate.reason_codes)))

        submitted = action == RiskAction.ALLOW
        if submitted:
            self.paper_engine.submit(order)
        return EquityPaperSubmissionResult(order, action, reasons, submitted)

    def on_closed_bar(self, bar: EquityBarEvent) -> tuple[PaperFillRecord, ...]:
        return self.paper_engine.on_bar(
            BarEvent(
                symbol=bar.meta.instrument_id,
                timestamp=bar.bar_end,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
        )
