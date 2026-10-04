from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

import pytest

from quant_system.research.r13_admission import R13AdmissionLedger
from quant_system.research.r13_certification import GenuineR13CertificationRunner
from quant_system.research.r13_evidence_pipeline import (
    PIPELINE_SCHEMA_ID,
    RR_IDS,
    R13EvidencePipelineError,
    R13HistoricalEvidencePipeline,
)
from quant_system.research.r13_manifest import canonical_json, validate_manifest_schema
from r13_evidence_pipeline_support import create_full_pass_source_bundle


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REAL_INPUT = (
    PROJECT_ROOT
    / "artifacts"
    / "research"
    / "r13_genuine_evidence"
    / "current_real_20260922_input.json"
)
REAL_REPLAY = "fba9ee4f70e0e5d8c937a0ac4ede37227e6bd0beb6d67720509504845c50e0ee"


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_frozen_h02_rr_catalogue_is_exact_and_canonically_hashed():
    path = PROJECT_ROOT / "schemas" / "h02_semantic_rules_v1.json"
    payload = _load(path)
    assert payload["catalogue_id"] == "EQS-H02-RR-CATALOGUE"
    assert payload["catalogue_version"] == "RR-001-030-v1"
    assert tuple(item["rule_id"] for item in payload["rules"]) == RR_IDS
    assert len(payload["rules"]) == 30
    canonical_hash = sha256(canonical_json(payload)).hexdigest()
    assert len(canonical_hash) == 64


def test_current_real_capture_fails_closed_with_deterministic_historical_blockers(tmp_path):
    result = R13HistoricalEvidencePipeline(project_root=PROJECT_ROOT).run(
        REAL_INPUT, tmp_path / "real"
    )
    assert result.status == "BLOCKED"
    assert result.certification_inputs_path is None
    status = _load(result.status_path)
    assert (
        status["dataset_manifest_fingerprint"]
        == "1db543b2881321a4e453915258f3d08926d0b8b066f4fde2c09d82e8a5d4016c"
    )
    assert (
        status["universe_fingerprint"]
        == "236009152ebffc20a6c59ef65662298c593f716d0d642a3aa8d7cfddb57066e3"
    )
    assert (status["rr_passed"], status["rr_failed"], status["rr_blocked"]) == (15, 1, 14)
    assert status["alpha_structurally_valid"] is True
    assert status["alpha_data_binding_ready"] is False
    assert status["certification_inputs_emitted"] is False
    assert "H02_WINDOW_SCOPE_MISMATCH" in status["blockers"]
    assert "H02_LIFECYCLE_GAP" in status["blockers"]
    assert "H02_LIQUIDITY_WINDOW_INVALID" in status["blockers"]


def test_current_real_capture_preserves_receipt_lineage_and_replay_fingerprint(tmp_path):
    result = R13HistoricalEvidencePipeline(project_root=PROJECT_ROOT).run(
        REAL_INPUT, tmp_path / "real"
    )
    source = _load(result.output_root / "source_receipt_index.json")
    assert source["receipt_count"] == 15
    assert source["all_hashes_verified"] is True
    assert all(item["verified"] is True for item in source["receipts"])

    h02 = _load(result.output_root / "h02_batch_manifest.json")
    assert h02["status"] == "BLOCKED"
    assert h02["replay_fingerprint"] == REAL_REPLAY

    rr = _load(result.output_root / "rr_results.json")
    rr5 = next(item for item in rr["results"] if item["rule_id"] == "RR-005")
    assert rr5["status"] == "PASS"
    assert rr5["observed"]["missing_lineage_hashes"] == []


def test_real_pipeline_is_byte_deterministic_for_same_input(tmp_path):
    pipeline = R13HistoricalEvidencePipeline(project_root=PROJECT_ROOT)
    first = pipeline.run(REAL_INPUT, tmp_path / "a")
    second = pipeline.run(REAL_INPUT, tmp_path / "b")
    names = (
        "rr_catalogue.json",
        "validator_build.json",
        "rr_results.json",
        "universe_snapshot.json",
        "lifecycle_event_set.json",
        "coverage_record_set.json",
        "eligibility_record_set.json",
        "pit_certification.json",
        "source_receipt_index.json",
        "protected_boundary_report.json",
        "alpha_binding.json",
        "h02_batch_manifest.json",
        "hash_index.json",
        "pipeline_status.json",
    )
    for name in names:
        assert (first.output_root / name).read_bytes() == (second.output_root / name).read_bytes()


def test_declared_input_hash_mismatch_fails_before_evidence_output(tmp_path):
    config = _load(REAL_INPUT)
    config["dataset_manifest"]["sha256"] = "0" * 64
    config_path = tmp_path / "bad_input.json"
    config_path.write_bytes(canonical_json(config))
    out = tmp_path / "out"

    with pytest.raises(R13EvidencePipelineError, match="hash mismatch"):
        R13HistoricalEvidencePipeline(project_root=PROJECT_ROOT).run(config_path, out)

    assert not out.exists()


def test_full_pass_commissioning_bundle_flows_through_manifest_runner(tmp_path):
    input_path = create_full_pass_source_bundle(tmp_path / "source")
    pipeline_result = R13HistoricalEvidencePipeline(project_root=PROJECT_ROOT).run(
        input_path, tmp_path / "evidence"
    )
    assert pipeline_result.status == "COMPLETE"
    assert pipeline_result.certification_inputs_path is not None

    status = _load(pipeline_result.status_path)
    assert (status["rr_passed"], status["rr_failed"], status["rr_blocked"]) == (30, 0, 0)
    assert status["alpha_structurally_valid"] is True
    assert status["alpha_data_binding_ready"] is True
    assert status["certification_inputs_emitted"] is True

    ledger = R13AdmissionLedger(tmp_path / "test_only_r13.sqlite")
    certification = GenuineR13CertificationRunner(project_root=PROJECT_ROOT).run(
        pipeline_result.certification_inputs_path,
        tmp_path / "canonical_manifest.json",
        admission_ledger=ledger,
        admit=True,
    )
    validate_manifest_schema(certification.manifest)
    assert certification.admission is not None
    assert certification.admission.generation == 1
    assert ledger.verify_hash_chain()
    ledger.close()


def test_pipeline_input_schema_identifier_is_frozen():
    assert PIPELINE_SCHEMA_ID == "EQS-R1.3-HISTORICAL-EVIDENCE-PIPELINE-INPUT-v1"
