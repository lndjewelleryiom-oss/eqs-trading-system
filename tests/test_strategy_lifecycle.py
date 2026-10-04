import pytest

from quant_system.performance import (
    PersistentStrategyLifecycle,
    StrategyLifecycleError,
    StrategyLifecycleState,
)


def test_synthetic_strategy_cannot_validate(tmp_path):
    lifecycle = PersistentStrategyLifecycle(tmp_path / "lifecycle.db")
    lifecycle.register(
        strategy_id="s1",
        strategy_version="v1",
        source_class="SYNTHETIC",
        research_trial_id="trial-1",
        research_evidence_sha256="a" * 64,
    )
    with pytest.raises(StrategyLifecycleError, match="SYNTHETIC_RESEARCH_CANNOT_VALIDATE"):
        lifecycle.transition(
            "s1",
            "v1",
            StrategyLifecycleState.VALIDATED,
            reasons=("SYNTHETIC_TEST_ONLY",),
            genuine_research_gate_passed=True,
        )


def test_genuine_strategy_must_follow_research_validated_canary_active(tmp_path):
    lifecycle = PersistentStrategyLifecycle(tmp_path / "lifecycle.db")
    rec = lifecycle.register(
        strategy_id="s2",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial-2",
        research_evidence_sha256="b" * 64,
        research_manifest_sha256="c" * 64,
    )
    assert rec.state is StrategyLifecycleState.RESEARCH

    rec = lifecycle.transition(
        "s2",
        "v1",
        StrategyLifecycleState.VALIDATED,
        reasons=("GENUINE_RESEARCH_PASS",),
        genuine_research_gate_passed=True,
    )
    assert rec.state is StrategyLifecycleState.VALIDATED

    rec = lifecycle.transition(
        "s2",
        "v1",
        StrategyLifecycleState.PAPER_CANARY,
        reasons=("CANARY_ADMISSION_PASS",),
        evidence_sha256="d" * 64,
    )
    assert rec.state is StrategyLifecycleState.PAPER_CANARY

    rec = lifecycle.transition(
        "s2",
        "v1",
        StrategyLifecycleState.PAPER_ACTIVE,
        reasons=("FORWARD_PAPER_ACCEPTANCE_PASS",),
        evidence_sha256="e" * 64,
    )
    assert rec.state is StrategyLifecycleState.PAPER_ACTIVE
    assert lifecycle.verify_hash_chain()


def test_cannot_skip_from_research_to_paper(tmp_path):
    lifecycle = PersistentStrategyLifecycle(tmp_path / "lifecycle.db")
    lifecycle.register(
        strategy_id="s3",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial-3",
        research_evidence_sha256="f" * 64,
    )
    with pytest.raises(StrategyLifecycleError, match="illegal transition"):
        lifecycle.transition(
            "s3",
            "v1",
            StrategyLifecycleState.PAPER_CANARY,
            reasons=("SKIP_ATTEMPT",),
        )


def test_paused_reentry_requires_revalidation_evidence(tmp_path):
    lifecycle = PersistentStrategyLifecycle(tmp_path / "lifecycle.db")
    lifecycle.register(
        strategy_id="s4",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial-4",
        research_evidence_sha256="1" * 64,
    )
    lifecycle.transition(
        "s4","v1",StrategyLifecycleState.VALIDATED,
        reasons=("PASS",), genuine_research_gate_passed=True,
    )
    lifecycle.transition(
        "s4","v1",StrategyLifecycleState.PAPER_CANARY,
        reasons=("CANARY",), evidence_sha256="2" * 64,
    )
    lifecycle.transition(
        "s4","v1",StrategyLifecycleState.PAUSED,
        reasons=("DEGRADATION",), evidence_sha256="3" * 64,
    )
    with pytest.raises(StrategyLifecycleError, match="PAUSED_REENTRY_REQUIRES_REVALIDATION_EVIDENCE"):
        lifecycle.transition(
            "s4","v1",StrategyLifecycleState.PAPER_CANARY,
            reasons=("TRY_REENTRY",),
        )


def test_live_state_does_not_exist():
    assert "LIVE" not in {state.value for state in StrategyLifecycleState}
