from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.core.enums import OrderType, RiskAction, Side
from quant_system.execution.models import OrderRequest
from quant_system.risk.policy import PortfolioRiskSnapshot, RiskEngine, RiskLimits


def limits():
    return RiskLimits(
        max_order_notional=Decimal("1000"), max_symbol_notional=Decimal("2500"),
        max_strategy_notional=Decimal("5000"), max_gross_notional=Decimal("10000"),
        max_leverage=Decimal("1"), max_daily_loss=Decimal("200"),
        max_drawdown_fraction=Decimal("0.05"), max_data_age=timedelta(seconds=5),
    )


def order(now, qty="1", px="100"):
    return OrderRequest(uuid4(), "XYZ", Side.BUY, Decimal(qty), OrderType.MARKET, now, Decimal(px))


def snapshot(now, **overrides):
    base = dict(
        equity=Decimal("10000"), peak_equity=Decimal("10000"), gross_notional=Decimal("0"),
        symbol_notional={}, strategy_notional={}, daily_pnl=Decimal("0"),
        market_data_received_at=now, global_kill_switch=False,
    )
    base.update(overrides)
    return PortfolioRiskSnapshot(**base)


def test_allows_order_within_limits():
    now = datetime.now(timezone.utc)
    assert RiskEngine(limits()).evaluate(order(now), snapshot(now)).action == RiskAction.ALLOW


def test_blocks_oversized_order():
    now = datetime.now(timezone.utc)
    decision = RiskEngine(limits()).evaluate(order(now, qty="11", px="100"), snapshot(now))
    assert decision.action == RiskAction.BLOCK
    assert "MAX_ORDER_NOTIONAL" in decision.reason_codes


def test_halts_on_stale_data():
    now = datetime.now(timezone.utc)
    decision = RiskEngine(limits()).evaluate(order(now), snapshot(now, market_data_received_at=now - timedelta(seconds=6)))
    assert decision.action == RiskAction.HALT
    assert decision.reason_codes == ("STALE_MARKET_DATA",)


def test_halts_on_kill_switch():
    now = datetime.now(timezone.utc)
    decision = RiskEngine(limits()).evaluate(order(now), snapshot(now, global_kill_switch=True))
    assert decision.action == RiskAction.HALT
