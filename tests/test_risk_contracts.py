from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
import pytest

from quant_system.risk.contracts import (
    CapitalMandate, LifecycleState, ReservationState, RiskDecisionV1,
    RiskDirection, RiskDisposition, RiskReservation,
)

NOW = datetime(2026, 9, 26, tzinfo=timezone.utc)
SID = uuid4()
IID = uuid4()

def mandate(**overrides):
    base = dict(mandate_id="m1", strategy_id=SID, strategy_version="v1", portfolio_id="p1",
        lifecycle_state=LifecycleState.PAPER, execution_authority=frozenset({"PAPER"}),
        permitted_venues=frozenset({"OKX"}), permitted_instruments=frozenset({"BTC-USDT-SWAP"}),
        capital_allocation=Decimal("100"), max_gross_exposure=Decimal("100"),
        max_net_exposure=Decimal("100"), max_position_exposure=Decimal("50"),
        max_order_notional=Decimal("10"), max_leverage=Decimal("1"), max_margin_usage=Decimal("50"),
        daily_loss_limit=Decimal("5"), drawdown_limit=Decimal(".1"), turnover_limit=Decimal("100"),
        order_rate_limit=10, concentration_limit=Decimal(".5"), protected_reserve=Decimal("20"),
        valid_from=NOW, valid_until=NOW+timedelta(days=1), review_at=NOW+timedelta(hours=12),
        issuer="EQS-00", version=1, reason_evidence_ref="e1", policy_version="risk-v1")
    base.update(overrides); return CapitalMandate(**base)

def test_mandate_is_immutable_and_fingerprinted():
    m=mandate(); assert len(m.fingerprint()) == 64
    with pytest.raises(Exception): m.version=2

def test_mandate_rejects_invalid_window():
    with pytest.raises(ValueError): mandate(valid_until=NOW-timedelta(seconds=1))

def test_rejected_decision_cannot_authorise_quantity():
    with pytest.raises(ValueError):
        RiskDecisionV1.create(intent_id=IID, strategy_id=SID, mandate_id="m1", mandate_version=1,
            input_state_ref="s1", disposition=RiskDisposition.REJECT, direction=RiskDirection.RISK_INCREASING,
            requested_quantity=Decimal("1"), authorised_quantity=Decimal("1"), reason_codes=("BLOCK",),
            limits_evaluated=("MANDATE",), limits_consumed=(), decided_at=NOW,
            risk_engine_version="v1", policy_version="risk-v1")

def test_reduce_decision_can_authorise_less():
    d=RiskDecisionV1.create(intent_id=IID, strategy_id=SID, mandate_id="m1", mandate_version=1,
        input_state_ref="s1", disposition=RiskDisposition.REDUCE, direction=RiskDirection.RISK_INCREASING,
        requested_quantity=Decimal("2"), authorised_quantity=Decimal("1"), reason_codes=("ORDER_CAP",),
        limits_evaluated=("ORDER",), limits_consumed=("ORDER",), decided_at=NOW,
        risk_engine_version="v1", policy_version="risk-v1")
    assert d.authorised_quantity == Decimal("1")

def test_unknown_execution_reservation_retains_capacity():
    r=RiskReservation(uuid4(),uuid4(),IID,"p1",SID,"BTC-USDT-SWAP","OKX",Decimal("10"),Decimal("0"),
        ReservationState.UNKNOWN_EXECUTION,NOW,NOW,None)
    assert r.reserved_notional == Decimal("10")

def test_unknown_execution_zero_reservation_is_invalid():
    with pytest.raises(ValueError):
        RiskReservation(uuid4(),uuid4(),IID,"p1",SID,"BTC-USDT-SWAP","OKX",Decimal("0"),Decimal("0"),
            ReservationState.UNKNOWN_EXECUTION,NOW,NOW,None)
