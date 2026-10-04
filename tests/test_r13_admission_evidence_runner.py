from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from r13_manifest_support import build_valid_manifest, refresh_manifest_hash

from quant_system.evolution import AcceptanceCriteria, ResearchEvidence
from quant_system.evolution.evidence_runner import (
    AlphaTrialEvidenceLedger,
    PersistentEvidenceRunner,
    SourceClass,
)
from quant_system.evolution.factory import StrategyBlueprint
from quant_system.research.r13_admission import (
    R13AdmissionError,
    R13AdmissionLedger,
    validate_r13_manifest,
)


def _blueprint() -> StrategyBlueprint:
    return StrategyBlueprint(
        hypothesis_id="hypothesis-1",
        family="test-family",
        asset_class="crypto-linear-perpetuals",
        horizon="intraday",
        signal_description="deterministic synthetic commissioning",
        falsification_tests=("oos", "walk-forward"),
    )


def _pass(_: StrategyBlueprint) -> ResearchEvidence:
    return ResearchEvidence(0.01, 0.8, 0.8, 0.99, 0.2, 0.01, True, True, 100)


def _fail(_: StrategyBlueprint) -> ResearchEvidence:
    return ResearchEvidence(-0.01, 0.2, 0.2, 0.60, 0.9, 0.5, True, False, 100)


def test_valid_r13_manifest_contract_accepts_all_pass_report(tmp_path):
    report = build_valid_manifest(tmp_path)
    binding = validate_r13_manifest(report, artifact_root=tmp_path)
    assert binding.manifest_sha256 == report["manifest_hash"]
    assert binding.dataset_id == "EQS-HIST-MULTIASSET-R1.3"
    assert len(binding.feature_run_manifest_fingerprints) == 1


def test_r13_manifest_fails_closed_when_certification_not_passed(tmp_path):
    report = build_valid_manifest(tmp_path)
    report["certification"]["status"] = "FAILED"
    refresh_manifest_hash(report)
    with pytest.raises(R13AdmissionError) as exc:
        validate_r13_manifest(report, artifact_root=tmp_path)
    assert exc.value.code == "R1_3_MANIFEST_INVALID"


def test_admission_is_immutable_hash_chained_and_restart_safe(tmp_path):
    path = tmp_path / "ledger.sqlite"
    report = build_valid_manifest(tmp_path)
    ledger = R13AdmissionLedger(path)
    record = ledger.admit(report)
    assert record.generation == 1
    assert ledger.active().admission_id == record.admission_id
    assert ledger.verify_hash_chain()
    ledger.close()

    reopened = R13AdmissionLedger(path)
    assert reopened.active().admission_id == record.admission_id
    assert reopened.verify_hash_chain()
    reopened.close()


def test_explicit_supersession_required_and_generation_increments(tmp_path):
    ledger = R13AdmissionLedger(tmp_path / "ledger.sqlite")
    first_report = build_valid_manifest(tmp_path)
    first = ledger.admit(first_report)

    second_report = deepcopy(first_report)
    second_report["manifest_id"] = "r13m_test0002"
    second_report["manifest_generation"] = 2
    second_report["dataset"]["dataset_version"] = "R1.3-test-g2"
    refresh_manifest_hash(second_report)
    with pytest.raises(R13AdmissionError) as exc:
        ledger.admit(second_report)
    assert exc.value.code == "R1_3_ADMISSION_CONFLICT"

    second = ledger.admit(second_report, supersedes_admission_id=first.admission_id)
    assert second.generation == 2
    assert ledger.status(first.admission_id) == "SUPERSEDED"
    assert ledger.active().admission_id == second.admission_id
    assert ledger.resolve_pinned_trial(first.admission_id, 1).admission_id == first.admission_id
    ledger.close()


def test_revocation_blocks_new_trial_selection_but_preserves_pin_lookup(tmp_path):
    ledger = R13AdmissionLedger(tmp_path / "ledger.sqlite")
    record = ledger.admit(build_valid_manifest(tmp_path))
    ledger.revoke(record.admission_id, reason="post-admission integrity concern")
    assert ledger.status(record.admission_id) == "REVOKED"
    with pytest.raises(R13AdmissionError) as exc:
        ledger.resolve_new_trial()
    assert exc.value.code == "R1_3_NOT_ADMITTED"
    assert ledger.resolve_pinned_trial(record.admission_id, record.generation) == record
    ledger.close()


def test_synthetic_runner_persists_pass_and_reject(tmp_path):
    ledger = AlphaTrialEvidenceLedger(tmp_path / "alpha.sqlite")
    criteria = AcceptanceCriteria()
    passing = PersistentEvidenceRunner(_pass, ledger)
    passing.run(_blueprint(), criteria=criteria)
    assert ledger.trial(passing.last_trial_id)["state"] == "PASSED"

    failing = PersistentEvidenceRunner(_fail, ledger)
    failing.run(_blueprint(), criteria=criteria)
    assert ledger.trial(failing.last_trial_id)["state"] == "REJECTED"
    assert ledger.verify_hash_chain()
    ledger.close()


def test_genuine_runner_blocks_without_r13_admission(tmp_path):
    alpha = AlphaTrialEvidenceLedger(tmp_path / "alpha.sqlite")
    admissions = R13AdmissionLedger(tmp_path / "admissions.sqlite")
    runner = PersistentEvidenceRunner(
        _pass,
        alpha,
        source_class=SourceClass.GENUINE,
        admission_ledger=admissions,
    )
    with pytest.raises(R13AdmissionError) as exc:
        runner.run(_blueprint(), criteria=AcceptanceCriteria())
    assert exc.value.code == "R1_3_NOT_ADMITTED"
    trial = alpha.trial(runner.last_trial_id)
    assert trial["state"] == "BLOCKED"
    assert trial["lifecycle_error_code"] == "R1_3_NOT_ADMITTED"
    alpha.close()
    admissions.close()


def test_genuine_runner_pins_full_admission_provenance(tmp_path):
    alpha = AlphaTrialEvidenceLedger(tmp_path / "alpha.sqlite")
    admissions = R13AdmissionLedger(tmp_path / "admissions.sqlite")
    admitted = admissions.admit(build_valid_manifest(tmp_path))
    runner = PersistentEvidenceRunner(
        _pass,
        alpha,
        source_class=SourceClass.GENUINE,
        admission_ledger=admissions,
    )
    runner.run(_blueprint(), criteria=AcceptanceCriteria())
    trial = alpha.trial(runner.last_trial_id)
    assert trial["state"] == "PASSED"
    assert trial["admission_id"] == admitted.admission_id
    assert trial["admission_generation"] == admitted.generation
    assert trial["manifest_sha256"] == admitted.manifest.manifest_sha256
    assert trial["dataset_manifest_fingerprint"] == admitted.manifest.dataset_manifest_fingerprint
    assert trial["universe_fingerprint"] == admitted.manifest.universe_fingerprint
    assert trial["h02_batch_manifest_sha256"] == admitted.manifest.h02_batch_manifest_sha256
    assert trial["lifecycle_event_set_sha256"] == admitted.manifest.lifecycle_event_set_sha256
    assert trial["coverage_record_set_sha256"] == admitted.manifest.coverage_record_set_sha256
    assert trial["eligibility_record_set_sha256"] == admitted.manifest.eligibility_record_set_sha256
    assert trial["rule_catalog_sha256"] == admitted.manifest.rule_catalog_sha256
    assert trial["pit_certification_sha256"] == admitted.manifest.pit_certification_sha256
    assert alpha.verify_hash_chain()
    alpha.close()
    admissions.close()


def test_manifest_hash_expectation_is_fail_closed(tmp_path):
    ledger = R13AdmissionLedger(tmp_path / "ledger.sqlite")
    with pytest.raises(R13AdmissionError) as exc:
        ledger.admit(build_valid_manifest(tmp_path), expected_manifest_sha256="0" * 64)
    assert exc.value.code == "R1_3_MANIFEST_HASH_MISMATCH"
    ledger.close()
