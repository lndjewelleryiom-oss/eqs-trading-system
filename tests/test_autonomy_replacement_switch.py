from decimal import Decimal

from executable_cert_support import genuine_fixture_certification
from quant_system.monitoring import ExpectedBehavior, ObservedBehavior
from quant_system.performance import (
    AutonomousPaperAllocator,
    PaperAllocationCandidate,
    PaperCanaryAdmissionLedger,
    PersistentStrategyLifecycle,
    StrategyLifecycleState,
)
from quant_system.performance.replacement import ReplacementResearchQueue
from quant_system.performance.replacement_worker import (
    ReplacementExecutionResult,
    ReplacementResearchWorker,
)
from quant_system.regime.models import MarketRegime


def candidate(strategy_id: str, weight: float = 0.10):
    return PaperAllocationCandidate(
        strategy_id=strategy_id,
        strategy_version="v1",
        researched_base_weight=weight,
        confidence=1.0,
        expected_volatility=0.10,
        drawdown_fraction=0.0,
        liquidity_score=1.0,
        capacity_notional=Decimal("100000"),
        regime_fit={MarketRegime.RISK_ON: 1.0},
    )


def test_pause_research_certify_canary_and_controlled_switch_mechanics(tmp_path):
    life = PersistentStrategyLifecycle(tmp_path / "life.db")

    old = life.register(
        strategy_id="old",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="old-trial",
        research_evidence_sha256="a" * 64,
        research_manifest_sha256="b" * 64,
        asset_class="crypto",
        family="momentum-trend",
    )
    life.transition(
        "old", "v1", StrategyLifecycleState.VALIDATED,
        reasons=("TEST_GENUINE_GATE_PASS",), genuine_research_gate_passed=True,
    )
    life.transition("old", "v1", StrategyLifecycleState.PAPER_CANARY, reasons=("CANARY",))
    life.transition("old", "v1", StrategyLifecycleState.PAPER_ACTIVE, reasons=("FORWARD_PASS",))
    life.transition("old", "v1", StrategyLifecycleState.PAUSED, reasons=("DEGRADED",))

    replacements = ReplacementResearchQueue(tmp_path / "replacements.db")
    request = replacements.enqueue_for_strategy(
        lifecycle=life,
        strategy_id="old",
        strategy_version="v1",
        reason="PERFORMANCE_DEGRADED",
    )
    worker = ReplacementResearchWorker(
        replacements, tmp_path / "replacement-worker.db", max_attempts=2
    )
    worker_outcome = worker.process_one(
        lambda req: ReplacementExecutionResult(
            "COMPLETE",
            "synthetic-fixture:replacement-research-evidence",
            "BOUNDED_FIXTURE_RESEARCH_COMPLETE",
        )
    )
    assert worker_outcome is not None
    assert worker_outcome.worker_result == "COMPLETE"

    new = life.register(
        strategy_id="replacement",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="replacement-fixture-trial",
        research_evidence_sha256="c" * 64,
        research_manifest_sha256="d" * 64,
        asset_class="crypto",
        family="momentum-trend",
        reasons=("INDEPENDENT_FIXTURE_ACCEPTANCE",),
    )
    life.transition(
        "replacement", "v1", StrategyLifecycleState.VALIDATED,
        reasons=("INDEPENDENT_FIXTURE_GATE_PASS",),
        evidence_sha256="c" * 64,
        genuine_research_gate_passed=True,
    )

    certifications, cert = genuine_fixture_certification(
        tmp_path,
        strategy_id="replacement",
        strategy_version="v1",
    )
    canary = PaperCanaryAdmissionLedger(
        tmp_path / "canary.db",
        interface_certifications=certifications,
    )
    canary.admit(
        lifecycle=life,
        strategy_id="replacement",
        strategy_version="v1",
        executable_sha256=cert.executable_sha256,
        interface_certification_sha256=cert.interface_certification_sha256,
        research_evidence_sha256="c" * 64,
        max_paper_weight=Decimal("0.01"),
        paper_authorised=True,
        broker_submission_enabled=False,
        live_authority=False,
        options_frozen_excluded=True,
    )

    allocation = AutonomousPaperAllocator(canary_weight_cap=0.02).allocate(
        lifecycle=life,
        candidates=[candidate("old"), candidate("replacement")],
        regime=MarketRegime.RISK_ON,
        regime_confidence=1.0,
        nav=Decimal("10000"),
    )
    assert allocation.result.weights["old"] == 0.0
    assert allocation.excluded_strategies["old"] == "LIFECYCLE_PAUSED"
    assert 0.0 < allocation.result.weights["replacement"] <= 0.02
    assert life.get("replacement", "v1").state is StrategyLifecycleState.PAPER_CANARY
    assert life.get("old", "v1").state is StrategyLifecycleState.PAUSED
    assert life.verify_hash_chain()
    assert replacements.verify_hash_chain()
    assert worker.verify()
