from __future__ import annotations

from decimal import Decimal

from quant_system.backtest.events import BarEvent
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator, ZERO
from quant_system.execution.models import OrderRequest


class ConservativeSpotCommodityBarExecutionSimulator(ConservativeBarExecutionSimulator):
    """Spot commodity simulator without fabricated consolidated exchange volume."""

    def _fillable_quantity(self, order: OrderRequest, bar: BarEvent) -> Decimal:
        if self.assumptions.partial_fill_fraction <= ZERO:
            return ZERO
        return order.quantity * self.assumptions.partial_fill_fraction

    def _impact_bps(self, fill_qty: Decimal, bar: BarEvent) -> Decimal:
        return ZERO
