from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from quant_system.core.enums import RiskAction
from quant_system.execution.models import OrderRequest


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_order_notional: Decimal
    max_symbol_notional: Decimal
    max_strategy_notional: Decimal
    max_gross_notional: Decimal
    max_leverage: Decimal
    max_daily_loss: Decimal
    max_drawdown_fraction: Decimal
    max_data_age: timedelta


@dataclass(frozen=True, slots=True)
class PortfolioRiskSnapshot:
    equity: Decimal
    peak_equity: Decimal
    gross_notional: Decimal
    symbol_notional: dict[str, Decimal]
    strategy_notional: dict[str, Decimal]
    daily_pnl: Decimal
    market_data_received_at: datetime
    global_kill_switch: bool = False


@dataclass(frozen=True, slots=True)
class RiskDecision:
    action: RiskAction
    reason_codes: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return self.action == RiskAction.ALLOW


class RiskEngine:
    """Authoritative, fail-closed pre-trade risk policy."""

    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def evaluate(self, order: OrderRequest, snapshot: PortfolioRiskSnapshot) -> RiskDecision:
        reasons: list[str] = []
        now = order.decision_time

        if snapshot.global_kill_switch:
            return RiskDecision(RiskAction.HALT, ("GLOBAL_KILL_SWITCH",))
        if snapshot.equity <= 0:
            return RiskDecision(RiskAction.HALT, ("NON_POSITIVE_EQUITY",))
        if snapshot.market_data_received_at > now:
            return RiskDecision(RiskAction.HALT, ("FUTURE_MARKET_DATA_TIMESTAMP",))
        if now - snapshot.market_data_received_at > self.limits.max_data_age:
            return RiskDecision(RiskAction.HALT, ("STALE_MARKET_DATA",))

        drawdown = (snapshot.peak_equity - snapshot.equity) / snapshot.peak_equity if snapshot.peak_equity > 0 else Decimal("1")
        if drawdown >= self.limits.max_drawdown_fraction:
            return RiskDecision(RiskAction.HALT, ("MAX_DRAWDOWN",))
        if snapshot.daily_pnl <= -self.limits.max_daily_loss:
            return RiskDecision(RiskAction.HALT, ("DAILY_LOSS_LIMIT",))

        order_notional = order.notional
        if order_notional > self.limits.max_order_notional:
            reasons.append("MAX_ORDER_NOTIONAL")

        current_symbol = snapshot.symbol_notional.get(order.symbol, Decimal("0"))
        if current_symbol + order_notional > self.limits.max_symbol_notional:
            reasons.append("MAX_SYMBOL_NOTIONAL")

        sid = str(order.strategy_id)
        current_strategy = snapshot.strategy_notional.get(sid, Decimal("0"))
        if current_strategy + order_notional > self.limits.max_strategy_notional:
            reasons.append("MAX_STRATEGY_NOTIONAL")

        new_gross = snapshot.gross_notional + order_notional
        if new_gross > self.limits.max_gross_notional:
            reasons.append("MAX_GROSS_NOTIONAL")
        if new_gross / snapshot.equity > self.limits.max_leverage:
            reasons.append("MAX_LEVERAGE")

        if reasons:
            return RiskDecision(RiskAction.BLOCK, tuple(reasons))
        return RiskDecision(RiskAction.ALLOW, ("WITHIN_LIMITS",))
