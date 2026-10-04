from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.core.enums import OrderType, Side
from quant_system.execution.models import OrderRequest
from quant_system.risk.authority import PreTradeAuthorityEngine, PreTradeRiskState
from quant_system.risk.contracts import CapitalMandate, LifecycleState, RiskDirection, RiskDisposition

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
SID = uuid4()

def mandate(**changes):
    base = dict(
        mandate_id="paper-synthetic-1", strategy_id=SID, strategy_version="fixture-v1",
        portfolio_id="PAPER-1", lifecycle_state=LifecycleState.PAPER,
        execution_authority=frozenset({"PAPER"}), permitted_venues=frozenset({"SYNTH"}),
        permitted_instruments=frozenset({"XYZ"}), capital_allocation=Decimal("10000"),
        max_gross_exposure=Decimal("5000"), max_net_exposure=Decimal("5000"),
        max_position_exposure=Decimal("2000"), max_order_notional=Decimal("1000"),
        max_leverage=Decimal("1"), max_margin_usage=Decimal("0.5"),
        daily_loss_limit=Decimal("500"), drawdown_limit=Decimal("0.1"),
        turnover_limit=Decimal("20000"), order_rate_limit=10,
        concentration_limit=Decimal("0.5"), protected_reserve=Decimal("1000"),
        valid_from=NOW-timedelta(hours=1), valid_until=NOW+timedelta(hours=1),
        review_at=NOW+timedelta(minutes=30), issuer="EQS-06-SYNTHETIC", version=1,
        reason_evidence_ref="synthetic-only", policy_version="eqs06-test-v1",
    )
    base.update(changes)
    return CapitalMandate(**base)

def state(**changes):
    base = dict(
        portfolio_id="PAPER-1", venue="SYNTH", instrument="XYZ",
        position_quantity=Decimal("0"), gross_exposure=Decimal("0"),
        net_exposure=Decimal("0"), margin_usage=Decimal("0"),
        daily_loss=Decimal("0"), drawdown=Decimal("0"), turnover=Decimal("0"),
        open_order_notional=Decimal("0"), reserved_notional=Decimal("0"),
        state_time=NOW, equity=Decimal("10000"), available_capital=Decimal("10000"),
        recent_order_count=0, truth_valid_until=NOW+timedelta(minutes=1),
        account_truth_known=True,
    )
    base.update(changes)
    return PreTradeRiskState(**base)

def order(qty="1", side=Side.BUY, reduce_only=False):
    return OrderRequest(
        SID, "XYZ", side, Decimal(qty), OrderType.MARKET, NOW,
        Decimal("100"), reduce_only=reduce_only,
    )

def test_valid_paper_mandate_allows_new_exposure():
    decision = PreTradeAuthorityEngine().evaluate(order(), mandate(), state())
    assert decision.disposition == RiskDisposition.ALLOW
    assert decision.direction == RiskDirection.RISK_INCREASING
    assert decision.authorised_quantity == Decimal("1")

def test_capacity_deterministically_reduces_quantity():
    decision = PreTradeAuthorityEngine().evaluate(
        order("20"), mandate(max_order_notional=Decimal("750")), state()
    )
    assert decision.disposition == RiskDisposition.REDUCE
    assert decision.authorised_quantity == Decimal("7.5")

def test_known_position_reduction_remains_distinct():
    decision = PreTradeAuthorityEngine().evaluate(
        order("2", Side.SELL, True), mandate(),
        state(position_quantity=Decimal("5"), gross_exposure=Decimal("500"))
    )
    assert decision.disposition == RiskDisposition.ALLOW
    assert decision.direction == RiskDirection.RISK_REDUCING
    assert decision.reason_codes == ("RISK_REDUCING",)

def test_unknown_safety_critical_state_fails_closed():
    decision = PreTradeAuthorityEngine().evaluate(
        order(), mandate(), state(gross_exposure=None)
    )
    assert decision.disposition == RiskDisposition.REJECT
    assert "SAFETY_CRITICAL_STATE_UNKNOWN" in decision.reason_codes

def test_unknown_position_cannot_claim_reduction():
    decision = PreTradeAuthorityEngine().evaluate(
        order("1", Side.SELL, True), mandate(), state(position_quantity=None)
    )
    assert decision.disposition == RiskDisposition.REJECT
    assert decision.direction == RiskDirection.RISK_INCREASING

def test_expired_or_mismatched_mandate_rejects():
    expired = mandate(valid_until=NOW-timedelta(minutes=1), review_at=NOW-timedelta(minutes=2))
    decision = PreTradeAuthorityEngine().evaluate(order(), expired, state())
    assert decision.disposition == RiskDisposition.REJECT
    assert "MANDATE_NOT_CURRENT" in decision.reason_codes

def test_loss_gate_blocks_new_exposure_but_not_known_reduction():
    blocked = PreTradeAuthorityEngine().evaluate(
        order(), mandate(), state(daily_loss=Decimal("500"))
    )
    reducing = PreTradeAuthorityEngine().evaluate(
        order("1", Side.SELL, True), mandate(),
        state(position_quantity=Decimal("2"), gross_exposure=Decimal("200"),
              daily_loss=Decimal("500"))
    )
    assert blocked.disposition == RiskDisposition.REJECT
    assert "DAILY_LOSS_LIMIT" in blocked.reason_codes
    assert reducing.disposition == RiskDisposition.ALLOW

def test_live_capital_is_unconditionally_disabled():
    live = mandate(
        lifecycle_state=LifecycleState.LIVE_PROBATION,
        execution_authority=frozenset({"LIVE_PROBATION"}),
    )
    decision = PreTradeAuthorityEngine().evaluate(order(), live, state())
    assert decision.disposition == RiskDisposition.REJECT
    assert "LIVE_CAPITAL_DISABLED" in decision.reason_codes
