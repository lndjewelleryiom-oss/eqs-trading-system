"""F7 pre-commissioning acceptance using no credentials and no live capital."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

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


def test_venue_agnostic_precommissioning_exit_gate():
    internal_evidence = CommissioningEvidence(
        all_required_components_passed=True,
        postgres_verified=True,
        shadow_acceptance_passed=True,
        risk_acceptance_passed=True,
    )
    readiness = assess_live_1_readiness(internal_evidence)
    assert not readiness.ready
    assert set(readiness.reasons) == {
        "TRADE_ONLY_CREDENTIALS_MISSING",
        "WITHDRAWALS_NOT_CONFIRMED_DISABLED",
        "REGULATORY_APPROVAL_MISSING",
        "OPERATIONAL_APPROVAL_MISSING",
        "HUMAN_LIVE_AUTHORIZATION_MISSING",
    }

    controller = CommissioningController()
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=True)
    boundary = CommissionedSubmissionBoundary(controller, gateway)
    test_order = OrderRequest(
        uuid4(), "SYNTH", Side.BUY, Decimal("1"), OrderType.MARKET,
        datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("100")
    )

    blocked = boundary.submit(test_order)
    assert not blocked.sent
    assert adapter.submitted == []
    assert controller.state == LiveLevel.LIVE_0

    # Synthetic full-evidence fixture is used only to exercise the state machine with
    # the in-memory broker double. It is not evidence that external gates are satisfied.
    controller.enter_live_1(CommissioningEvidence(True, True, True, True, True, True, True, True, True))
    controller.scale_one_level(ScaleEvidence(10, 10, True, True, True, True))
    assert controller.state == LiveLevel.LIVE_2

    soft = controller.apply_runtime_safety(RuntimeSafetyEvidence(degradation_clear=False))
    assert soft.target == LiveLevel.LIVE_1
    assert controller.state == LiveLevel.LIVE_1

    critical = controller.apply_runtime_safety(RuntimeSafetyEvidence(global_kill_switch_clear=False))
    assert critical.target == LiveLevel.LIVE_0
    assert controller.state == LiveLevel.LIVE_0

    blocked_again = boundary.submit(test_order)
    assert not blocked_again.sent
    assert adapter.submitted == []
