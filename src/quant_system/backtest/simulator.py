from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from quant_system.backtest.accounting import Ledger
from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.core.enums import OrderType, Side
from quant_system.execution.models import OrderRequest


ZERO = Decimal("0")
BPS = Decimal("10000")


@dataclass(frozen=True, slots=True)
class SimulatorState:
    pending_orders: tuple[OrderRequest, ...]
    seen_order_ids: tuple[object, ...]
    last_bar_times: dict[str, object]


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    order_id: object
    symbol: str
    quantity: Decimal
    price: Decimal
    commission: Decimal
    timestamp: object
    spread_bps_applied: Decimal = ZERO
    slippage_bps_applied: Decimal = ZERO
    impact_bps_applied: Decimal = ZERO


class ConservativeBarExecutionSimulator:
    """Deterministic conservative OHLCV execution model.

    Market orders:
    - never fill on the decision timestamp;
    - cannot fill before decision_time + configured latency;
    - start from the next eligible bar open;
    - pay adverse half-spread, slippage and participation-scaled market impact.

    Limit orders:
    - cannot fill before latency or on the decision timestamp;
    - require the bar range to trade through/touch the limit;
    - fill at the limit, never at an optimistic price improvement;
    - do not add price slippage beyond the limit because that would violate the order.
      Queue position is unknown in OHLCV, so fills remain capped by participation and
      partial-fill assumptions; higher-fidelity queue modelling belongs to tick/L2 mode.
    """

    def __init__(
        self,
        assumptions: ExecutionAssumptions,
        *,
        max_volume_participation: Decimal = Decimal("0.05"),
    ) -> None:
        if not ZERO < max_volume_participation <= Decimal("1"):
            raise ValueError("max_volume_participation must be in (0,1]")
        self.assumptions = assumptions
        self.max_volume_participation = max_volume_participation
        self._pending: dict[object, OrderRequest] = {}
        self._seen: set[object] = set()
        self._last_bar_time: dict[str, object] = {}

    def submit(self, order: OrderRequest) -> None:
        if order.order_id in self._seen:
            raise ValueError("duplicate order submission")
        self._seen.add(order.order_id)
        self._pending[order.order_id] = order

    def _is_eligible(self, order: OrderRequest, bar: BarEvent) -> bool:
        if order.symbol != bar.symbol or bar.timestamp <= order.decision_time:
            return False
        eligible_at = order.decision_time + timedelta(milliseconds=self.assumptions.latency_ms)
        return bar.timestamp >= eligible_at

    def _limit_touched(self, order: OrderRequest, bar: BarEvent) -> bool:
        assert order.limit_price is not None
        if order.side == Side.BUY:
            return bar.low <= order.limit_price
        return bar.high >= order.limit_price

    def _fillable_quantity(self, order: OrderRequest, bar: BarEvent) -> Decimal:
        if bar.volume <= ZERO or self.assumptions.partial_fill_fraction <= ZERO:
            return ZERO
        max_by_volume = bar.volume * self.max_volume_participation
        return min(order.quantity * self.assumptions.partial_fill_fraction, max_by_volume)

    def _impact_bps(self, fill_qty: Decimal, bar: BarEvent) -> Decimal:
        if self.assumptions.impact_bps == ZERO or bar.volume <= ZERO or fill_qty <= ZERO:
            return ZERO
        participation = fill_qty / bar.volume
        # impact_bps is interpreted as impact at max configured participation.
        # Square-root scaling is a standard conservative stylization for impact.
        relative_participation = min(Decimal("1"), participation / self.max_volume_participation)
        return self.assumptions.impact_bps * relative_participation.sqrt()

    def on_bar(self, bar: BarEvent) -> tuple[tuple[OrderRequest, SimulatedFill], ...]:
        previous = self._last_bar_time.get(bar.symbol)
        if previous is not None and bar.timestamp <= previous:
            raise ValueError("bar timestamps must be strictly increasing per symbol")
        self._last_bar_time[bar.symbol] = bar.timestamp

        results: list[tuple[OrderRequest, SimulatedFill]] = []
        for order_id, order in list(self._pending.items()):
            if not self._is_eligible(order, bar):
                continue
            if order.order_type == OrderType.LIMIT and not self._limit_touched(order, bar):
                continue

            fill_qty = self._fillable_quantity(order, bar)
            if fill_qty <= ZERO:
                continue

            spread_bps = ZERO
            slippage_bps = ZERO
            impact_bps = ZERO
            if order.order_type == OrderType.MARKET:
                spread_bps = self.assumptions.spread_bps / Decimal("2")
                slippage_bps = self.assumptions.slippage_bps
                impact_bps = self._impact_bps(fill_qty, bar)
                adverse = (spread_bps + slippage_bps + impact_bps) / BPS
                if order.side == Side.BUY:
                    price = bar.open * (Decimal("1") + adverse)
                else:
                    price = bar.open * (Decimal("1") - adverse)
                if price <= ZERO:
                    raise ValueError("execution assumptions imply a non-positive fill price")
            elif order.order_type == OrderType.LIMIT:
                assert order.limit_price is not None
                price = order.limit_price
            else:  # fail closed for future order types
                raise NotImplementedError(f"unsupported order type: {order.order_type}")

            commission = fill_qty * price * self.assumptions.commission_bps / BPS
            fill = SimulatedFill(
                order_id=order_id,
                symbol=order.symbol,
                quantity=fill_qty,
                price=price,
                commission=commission,
                timestamp=bar.timestamp,
                spread_bps_applied=spread_bps,
                slippage_bps_applied=slippage_bps,
                impact_bps_applied=impact_bps,
            )
            results.append((order, fill))

            remaining = order.quantity - fill_qty
            if remaining <= ZERO:
                del self._pending[order_id]
            else:
                self._pending[order_id] = OrderRequest(
                    strategy_id=order.strategy_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=remaining,
                    order_type=order.order_type,
                    decision_time=order.decision_time,
                    reference_price=order.reference_price,
                    limit_price=order.limit_price,
                    reduce_only=order.reduce_only,
                    order_id=order.order_id,
                )
        return tuple(results)

    def export_state(self) -> SimulatorState:
        return SimulatorState(
            pending_orders=tuple(self._pending.values()),
            seen_order_ids=tuple(sorted(self._seen, key=str)),
            last_bar_times=dict(self._last_bar_time),
        )

    def restore_state(self, state: SimulatorState) -> None:
        pending = {order.order_id: order for order in state.pending_orders}
        seen = set(state.seen_order_ids)
        if not set(pending).issubset(seen):
            raise ValueError("simulator pending orders must be present in seen order ids")
        self._pending = pending
        self._seen = seen
        self._last_bar_time = dict(state.last_bar_times)

    def has_seen_order(self, order_id: object) -> bool:
        return order_id in self._seen

    @property
    def pending_count(self) -> int:
        return len(self._pending)
