from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from quant_system.commissioning import (
    CommissionedSubmissionBoundary,
    CommissioningController,
    CommissioningEvidence,
    LiveLevel,
    RuntimeSafetyEvidence,
    ScaleEvidence,
    assess_live_1_readiness,
)
from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest


def fully_ready():
    return CommissioningEvidence(True, True, True, True, True, True, True, True, True)


def software_ready_external_blocked():
    return CommissioningEvidence(
        all_required_components_passed=True,
        postgres_verified=True,
        trade_only_credentials_present=False,
        withdrawals_disabled=False,
        regulatory_approved=False,
        operational_approved=False,
        human_live_authorized=False,
        shadow_acceptance_passed=True,
        risk_acceptance_passed=True,
    )


def order():
    return OrderRequest(
        uuid4(), "ABC", Side.BUY, Decimal("1"), OrderType.MARKET,
        datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("100")
    )


def good_scale():
    return ScaleEvidence(100, 100, True, True, True, True)


def test_default_readiness_is_blocked_on_every_human_external_gate():
    decision = assess_live_1_readiness(CommissioningEvidence())
    assert not decision.ready
    assert "POSTGRES_NOT_VERIFIED" in decision.reasons
    assert "HUMAN_LIVE_AUTHORIZATION_MISSING" in decision.reasons
    assert "TRADE_ONLY_CREDENTIALS_MISSING" in decision.reasons


def test_completed_software_gates_leave_only_external_live_gates():
    decision = assess_live_1_readiness(software_ready_external_blocked())
    assert not decision.ready
    assert set(decision.reasons) == {
        "TRADE_ONLY_CREDENTIALS_MISSING",
        "WITHDRAWALS_NOT_CONFIRMED_DISABLED",
        "REGULATORY_APPROVAL_MISSING",
        "OPERATIONAL_APPROVAL_MISSING",
        "HUMAN_LIVE_AUTHORIZATION_MISSING",
    }


def test_controller_cannot_leave_live_0_without_full_evidence():
    controller = CommissioningController()
    with pytest.raises(PermissionError):
        controller.enter_live_1(CommissioningEvidence(all_required_components_passed=True))
    assert controller.state == LiveLevel.LIVE_0


def test_scaling_is_one_level_at_a_time_and_requires_evidence():
    controller = CommissioningController()
    controller.enter_live_1(fully_ready())
    bad = ScaleEvidence(100, 99, True, True, True, True)
    with pytest.raises(PermissionError):
        controller.scale_one_level(bad)
    assert controller.state == LiveLevel.LIVE_1
    assert controller.scale_one_level(good_scale()) == LiveLevel.LIVE_2
    assert controller.scale_one_level(good_scale()) == LiveLevel.LIVE_3
    assert controller.scale_one_level(good_scale()) == LiveLevel.LIVE_4
    with pytest.raises(ValueError):
        controller.scale_one_level(good_scale())


def test_scale_requires_positive_precommitted_observation_floor():
    evidence = ScaleEvidence(0, 0, True, True, True, True)
    assert not evidence.passed


def test_rollback_can_immediately_deactivate_live_state():
    controller = CommissioningController()
    controller.enter_live_1(fully_ready())
    controller.scale_one_level(good_scale())
    assert controller.rollback("drawdown breach") == LiveLevel.LIVE_0
    assert controller.history[-1][1] == "ROLLBACK:drawdown breach"


def test_rollback_requires_reason_and_cannot_increase_state():
    controller = CommissioningController()
    with pytest.raises(ValueError):
        controller.rollback("", target=LiveLevel.LIVE_0)
    with pytest.raises(ValueError):
        controller.rollback("invalid escalation", target=LiveLevel.LIVE_1)


def test_critical_runtime_fault_auto_derisks_to_live_0():
    controller = CommissioningController()
    controller.enter_live_1(fully_ready())
    controller.scale_one_level(good_scale())
    decision = controller.apply_runtime_safety(RuntimeSafetyEvidence(reconciliation_clean=False))
    assert not decision.safe
    assert decision.target == LiveLevel.LIVE_0
    assert controller.state == LiveLevel.LIVE_0
    assert "RECONCILIATION_FAILURE" in decision.reasons
    assert controller.history[-1][1].startswith("AUTO_DERISK:")


def test_execution_degradation_reduces_exactly_one_live_level():
    controller = CommissioningController()
    controller.enter_live_1(fully_ready())
    controller.scale_one_level(good_scale())
    controller.scale_one_level(good_scale())
    assert controller.state == LiveLevel.LIVE_3
    decision = controller.apply_runtime_safety(RuntimeSafetyEvidence(execution_quality_within_limit=False))
    assert decision.target == LiveLevel.LIVE_2
    assert controller.state == LiveLevel.LIVE_2


def test_safety_fault_while_live_0_remains_disabled():
    controller = CommissioningController()
    decision = controller.apply_runtime_safety(RuntimeSafetyEvidence(market_data_fresh=False))
    assert not decision.safe
    assert controller.state == LiveLevel.LIVE_0
    assert controller.history[-1][1].startswith("SAFETY_BLOCK:")


def test_commissioned_submission_boundary_never_touches_adapter_in_live_0():
    controller = CommissioningController()
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=True)
    boundary = CommissionedSubmissionBoundary(controller, gateway)
    result = boundary.submit(order())
    assert not result.sent
    assert result.reason == "COMMISSIONING_LIVE_0"
    assert adapter.submitted == []


def test_commissioned_submission_boundary_uses_in_memory_adapter_only_after_live_gate():
    controller = CommissioningController()
    controller.enter_live_1(fully_ready())
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=True)
    boundary = CommissionedSubmissionBoundary(controller, gateway)
    result = boundary.submit(order())
    assert result.sent
    assert len(adapter.submitted) == 1
    assert adapter.submitted[0].symbol == "ABC"
