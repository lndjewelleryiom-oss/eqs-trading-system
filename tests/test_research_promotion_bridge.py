from quant_system.evolution.evidence_runner import (
    AlphaTrialEvidenceLedger,
    SourceClass,
    TrialState,
)
from quant_system.evolution.factory import StrategyBlueprint
from quant_system.evolution.models import AcceptanceCriteria, ResearchEvidence
from quant_system.performance import PersistentStrategyLifecycle, StrategyLifecycleState
from quant_system.performance.promotion import (
    ResearchPromotionError,
    promote_passed_genuine_trial,
)
from quant_system.research.r13_admission import AdmissionRecord, R13ManifestBinding

import pytest


def _blueprint(hypothesis_id="h1"):
    return StrategyBlueprint(
        hypothesis_id=hypothesis_id,
        family="event-response",
        asset_class="crypto",
        horizon="1h",
        signal_description="test",
        falsification_tests=("oos",),
    )


def _passing_evidence():
    return ResearchEvidence(
        oos_mean_after_costs=0.01,
        walk_forward_positive_fraction=0.8,
        parameter_robust_fraction=0.8,
        dsr_probability=0.99,
        pbo=0.1,
        reality_check_p=0.01,
        leakage_checks_passed=True,
        cost_stress_passed=True,
        evidence_count=100,
    )


def _admission():
    h = "a" * 64
    manifest = R13ManifestBinding(
        manifest_sha256=h,
        binding_id=h,
        dataset_id="dataset",
        dataset_manifest_fingerprint="b" * 64,
        universe_fingerprint="c" * 64,
        event_identity_hash="d" * 64,
        replay_fingerprint="e" * 64,
        feature_engine_version="v1",
        feature_run_manifest_fingerprints=("f" * 64,),
        decision_time="2026-09-21T00:00:00Z",
        venues=("BINANCE_USDM",),
        h02_batch_manifest_sha256="1" * 64,
        universe_snapshot_sha256="2" * 64,
        lifecycle_event_schema_version="v1",
        lifecycle_event_set_sha256="3" * 64,
        coverage_record_set_sha256="4" * 64,
        eligibility_record_set_sha256="5" * 64,
        validator_version="v1",
        rule_catalog_version="v1",
        rule_catalog_sha256="6" * 64,
        pit_certification_sha256="7" * 64,
    )
    return AdmissionRecord(
        admission_id="adm-1",
        generation=1,
        admitted_at="2026-09-21T00:00:00Z",
        manifest=manifest,
        manifest_json="{}",
    )


def _passed_trial(path, *, source_class, trial_id, hypothesis_id, admission):
    ledger = AlphaTrialEvidenceLedger(path)
    ledger.create_trial(
        trial_id=trial_id,
        blueprint=_blueprint(hypothesis_id),
        source_class=source_class,
        criteria=AcceptanceCriteria(),
        admission=admission,
    )
    ledger.transition(trial_id, TrialState.RUNNING)
    ledger.persist_evidence(
        trial_id,
        _passing_evidence(),
        ("ALL_PRECOMMITTED_CHECKS_PASSED",),
    )
    ledger.transition(trial_id, TrialState.PASSED)
    ledger.close()


def test_synthetic_passed_trial_cannot_promote(tmp_path):
    alpha = tmp_path / "alpha.db"
    _passed_trial(
        alpha,
        source_class=SourceClass.SYNTHETIC,
        trial_id="trial-s",
        hypothesis_id="synthetic-h",
        admission=None,
    )
    lifecycle = PersistentStrategyLifecycle(tmp_path / "life.db")
    with pytest.raises(ResearchPromotionError, match="SYNTHETIC_TRIAL_NOT_PROMOTABLE"):
        promote_passed_genuine_trial(
            alpha_ledger_path=alpha,
            trial_id="trial-s",
            lifecycle=lifecycle,
        )


def test_genuine_passed_trial_enters_validated_lifecycle(tmp_path):
    alpha = tmp_path / "alpha.db"
    _passed_trial(
        alpha,
        source_class=SourceClass.GENUINE,
        trial_id="trial-g",
        hypothesis_id="genuine-h",
        admission=_admission(),
    )
    lifecycle = PersistentStrategyLifecycle(tmp_path / "life.db")
    result = promote_passed_genuine_trial(
        alpha_ledger_path=alpha,
        trial_id="trial-g",
        lifecycle=lifecycle,
        strategy_version="candidate-v1",
    )
    assert result.lifecycle_state is StrategyLifecycleState.VALIDATED
    rec = lifecycle.get("genuine-h", "candidate-v1")
    assert rec.source_class == "GENUINE"
    assert rec.research_trial_id == "trial-g"
    assert rec.research_evidence_sha256 == result.evidence_sha256
    assert lifecycle.verify_hash_chain()
