from __future__ import annotations

from decimal import Decimal

from quant_system.backtest.events import BarEvent
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator, ZERO
from quant_system.execution.models import OrderRequest


class ConservativeFxBarExecutionSimulator(ConservativeBarExecutionSimulator):
    """Conservative spot-FX bar simulator without fabricated exchange volume.

    Spot FX has no authoritative consolidated exchange volume. This simulator therefore:
    - preserves the base simulator's next-bar/latency rule;
    - applies adverse half-spread and configured slippage;
    - never invents volume participation or market-impact estimates;
    - uses the configured partial_fill_fraction only.
    """

    def _fillable_quantity(self, order: OrderRequest, bar: BarEvent) -> Decimal:
        if self.assumptions.partial_fill_fraction <= ZERO:
            return ZERO
        return order.quantity * self.assumptions.partial_fill_fraction

    def _impact_bps(self, fill_qty: Decimal, bar: BarEvent) -> Decimal:
        return ZERO
