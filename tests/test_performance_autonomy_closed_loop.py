from decimal import Decimal

from executable_cert_support import genuine_fixture_certification
from quant_system.monitoring import ExpectedBehavior, ObservedBehavior
from quant_system.performance import (
    AutonomousPaperAllocator,
    ForwardPaperEvidence,
    PaperAllocationCandidate,
    PaperCanaryAdmissionLedger,
    PaperPerformanceController,
    PersistentStrategyLifecycle,
    ReplacementResearchQueue,
    StrategyLifecycleState,
)
from quant_system.regime.models import MarketRegime


def test_closed_loop_mechanics_are_paper_only_and_fail_closed(tmp_path):
    life = PersistentStrategyLifecycle(tmp_path / "life.db")
    rec = life.register(
        strategy_id="fixture-strategy",
        strategy_version="fixture-v1",
        source_class="GENUINE",
        research_trial_id="fixture-genuine-trial",
        research_evidence_sha256="a" * 64,
        research_manifest_sha256="b" * 64,
        asset_class="crypto",
        family="event-response",
        horizon="1h",
        signal_description="fixture only",
    )
    life.transition(
        rec.strategy_id,
        rec.strategy_version,
        StrategyLifecycleState.VALIDATED,
        reasons=("TEST_FIXTURE_GENUINE_GATE_PASS",),
        genuine_research_gate_passed=True,
    )

    certifications, cert = genuine_fixture_certification(
        tmp_path,
        strategy_id=rec.strategy_id,
        strategy_version=rec.strategy_version,
    )
    canary = PaperCanaryAdmissionLedger(
        tmp_path / "canary.db",
        interface_certifications=certifications,
    )
    canary.admit(
        lifecycle=life,
        strategy_id=rec.strategy_id,
        strategy_version=rec.strategy_version,
        executable_sha256=cert.executable_sha256,
        interface_certification_sha256=cert.interface_certification_sha256,
        research_evidence_sha256="a" * 64,
        max_paper_weight=Decimal("0.01"),
        paper_authorised=True,
        broker_submission_enabled=False,
        live_authority=False,
        options_frozen_excluded=True,
    )

    expected = ExpectedBehavior(0.55, 1.0, 1.5, 0.10, 5.0, 30.0)
    good = ObservedBehavior(0.54, 0.95, 1.45, 0.04, 5.2, 29.0)
    controller = PaperPerformanceController(tmp_path / "control.db")
    promoted = controller.evaluate(
        lifecycle=life,
        strategy_id=rec.strategy_id,
        strategy_version=rec.strategy_version,
        expected=expected,
        observed=good,
        evidence=ForwardPaperEvidence(100, 30, 48.0, "e" * 64),
    )
    assert promoted.resulting_state is StrategyLifecycleState.PAPER_ACTIVE

    bad = ObservedBehavior(0.10, 0.10, 0.10, 0.20, 25.0, 5.0)
    paused = controller.evaluate(
        lifecycle=life,
        strategy_id=rec.strategy_id,
        strategy_version=rec.strategy_version,
        expected=expected,
        observed=bad,
        evidence=ForwardPaperEvidence(150, 50, 72.0, "f" * 64),
    )
    assert paused.resulting_state is StrategyLifecycleState.PAUSED

    replacement = ReplacementResearchQueue(tmp_path / "replacement.db")
    req = replacement.enqueue_for_strategy(
        lifecycle=life,
        strategy_id=rec.strategy_id,
        strategy_version=rec.strategy_version,
        reason="PERFORMANCE_PAUSED",
    )
    assert req.asset_class == "crypto"
    assert req.family == "event-response"

    allocator = AutonomousPaperAllocator()
    allocation = allocator.allocate(
        lifecycle=life,
        candidates=[
            PaperAllocationCandidate(
                strategy_id=rec.strategy_id,
                strategy_version=rec.strategy_version,
                researched_base_weight=0.10,
                confidence=1.0,
                expected_volatility=0.10,
                drawdown_fraction=0.0,
                liquidity_score=1.0,
                capacity_notional=Decimal("100000"),
                regime_fit={MarketRegime.RISK_ON: 1.0},
            )
        ],
        regime=MarketRegime.RISK_ON,
        regime_confidence=1.0,
        nav=Decimal("10000"),
    )
    assert allocation.result.weights[rec.strategy_id] == 0.0
    assert life.verify_hash_chain()
    assert controller.verify_hash_chain()
    assert replacement.verify_hash_chain()
    assert "LIVE" not in {state.value for state in StrategyLifecycleState}
