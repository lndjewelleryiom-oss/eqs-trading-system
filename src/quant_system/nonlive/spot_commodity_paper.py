from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from uuid import UUID, uuid5

from quant_system.backtest.events import BarEvent
from quant_system.core.enums import OrderType, RiskAction, Side
from quant_system.data.commodities.models import SpotCommodityBar
from quant_system.execution.models import OrderRequest
from quant_system.paper.engine import PaperFillRecord, PaperTradingEngine
from quant_system.risk.policy import PortfolioRiskSnapshot, RiskEngine
from quant_system.risk.shared03 import Shared03Context, Shared03GateDisposition, Shared03NonLiveGate

_ORDER_NAMESPACE=UUID("c23dc1d6-536b-598e-a938-c7ba07e93b7c")


@dataclass(frozen=True, slots=True)
class SpotCommodityPaperSubmissionResult:
    order: OrderRequest
    action: RiskAction
    reason_codes: tuple[str,...]
    submitted_to_paper_engine: bool


class SpotCommodityPaperAdapter:
    def __init__(self, *, paper_engine: PaperTradingEngine, risk_engine: RiskEngine,
                 shared03_gate: Shared03NonLiveGate | None=None) -> None:
        self.paper_engine=paper_engine
        self.risk_engine=risk_engine
        self.shared03_gate=shared03_gate or Shared03NonLiveGate()

    @staticmethod
    def order_from_closed_bar(
        bar: SpotCommodityBar, *, strategy_id: UUID, side: Side, quantity: Decimal
    ) -> OrderRequest:
        payload=f"{strategy_id}|{bar.instrument_id}|{side.value}|{quantity}|{bar.canonical_identity()}"
        return OrderRequest(
            strategy_id=strategy_id,symbol=bar.instrument_id,side=side,quantity=quantity,
            order_type=OrderType.MARKET,decision_time=bar.available_at,
            reference_price=bar.close,order_id=uuid5(_ORDER_NAMESPACE,payload)
        )

    def evaluate_and_submit(
        self, bar: SpotCommodityBar, order: OrderRequest, *,
        risk_snapshot: PortfolioRiskSnapshot, shared03_context: Shared03Context | None
    ) -> SpotCommodityPaperSubmissionResult:
        if order.symbol!=bar.instrument_id:
            raise ValueError("order symbol must match spot commodity bar instrument_id")
        if order.reference_price!=bar.close:
            raise ValueError("order reference_price must equal spot commodity bar close")
        if order.decision_time<bar.available_at:
            raise ValueError("order decision_time precedes source availability")

        snap=replace(risk_snapshot,market_data_received_at=bar.available_at)
        raw=self.risk_engine.evaluate(order,snap)
        action=raw.action
        reasons=raw.reason_codes
        if raw.action==RiskAction.ALLOW:
            if shared03_context is None:
                action=RiskAction.HALT
                reasons=tuple(sorted(set(reasons+("SHARED03_CONTEXT_MISSING",))))
            elif shared03_context.execution_mode!="PAPER":
                action=RiskAction.HALT
                reasons=tuple(sorted(set(reasons+("SHARED03_EXECUTION_MODE_MISMATCH",))))
            else:
                gate=self.shared03_gate.evaluate(order,shared03_context)
                if gate.disposition==Shared03GateDisposition.HALT:
                    action=RiskAction.HALT
                elif gate.disposition==Shared03GateDisposition.BLOCK:
                    action=RiskAction.BLOCK
                reasons=tuple(sorted(set(reasons+gate.reason_codes)))
        submitted=action==RiskAction.ALLOW
        if submitted:
            self.paper_engine.submit(order)
        return SpotCommodityPaperSubmissionResult(order,action,reasons,submitted)

    def on_closed_bar(self, bar: SpotCommodityBar) -> tuple[PaperFillRecord,...]:
        return self.paper_engine.on_bar(BarEvent(
            symbol=bar.instrument_id,timestamp=bar.bar_end,open=bar.open,high=bar.high,
            low=bar.low,close=bar.close,volume=Decimal("0")
        ))
