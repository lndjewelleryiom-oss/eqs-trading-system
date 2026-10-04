from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from eqs_srf_validator import canonical_sha256, seal_object
from quant_system.research.srf_v1 import (
    CanonicalSRFResearchWorkflow,
    ImmutableEvidenceError,
    SRFIntegrationError,
)

EVALUATED_AT = "2026-09-27T18:45:00+00:00"


def _pass_graph():
    manifest = {"dataset_id": "DATASET-CERTIFIED-1", "rows": 100, "pit": True}
    source_hash = "a" * 64
    obj = {
        "object_type": "EQSDataBinding",
        "schema_version": "EQS-DATA-BINDING-1.0",
        "data_binding_id": "DATA-BINDING-CERTIFIED-1",
        "market_data_certification_ids": ["CERT-PIT-1"],
        "dataset_manifest_hash": canonical_sha256(manifest),
        "dataset_schema_versions": {"quotes": "1.0"},
        "source_snapshot_hashes": [source_hash],
        "calendar_versions": {"UTC": "2026.1"},
        "reference_data_versions": {"security_master": "1.0"},
        "pit_certification_status": "CERTIFIED",
        "bound_at": "2026-09-27T18:40:00+00:00",
        "binding_hash": "0" * 64,
    }
    obj = seal_object(obj, "binding_hash")
    evidence = {
        "market_data_certifications": ["CERT-PIT-1"],
        "dataset_manifests": {obj["data_binding_id"]: manifest},
        "source_snapshot_hashes": {obj["data_binding_id"]: [source_hash]},
    }
    return [obj], evidence


def test_positive_control_visits_all_89_rules_and_persists(tmp_path: Path):
    objects, evidence = _pass_graph()
    workflow = CanonicalSRFResearchWorkflow(tmp_path / "srf")
    outcome = workflow.validate_and_persist(objects, evidence, evaluated_at=EVALUATED_AT)
    assert outcome.aggregate_outcome == "PASS"
    assert outcome.bundle.visited_catalog_rule_count == 89
    assert len(outcome.bundle.executed_rule_ids) == 89
    assert len(outcome.bundle.results) == 89
    ok, failures = workflow.verify_store()
    assert ok, failures
    assert outcome.run_manifest["authority_boundary"] == {
        "research_handoff_only": True,
        "paper_authorized": False,
        "live_authorized": False,
        "programme_gate_authority": "EQS-00",
    }


def test_fixed_time_replay_is_deterministic_and_idempotent(tmp_path: Path):
    objects, evidence = _pass_graph()
    workflow = CanonicalSRFResearchWorkflow(tmp_path / "srf")
    first = workflow.validate_and_persist(objects, evidence, evaluated_at=EVALUATED_AT)
    second = workflow.validate_and_persist(objects, evidence, evaluated_at=EVALUATED_AT)
    assert first.bundle.report == second.bundle.report
    assert first.run_manifest == second.run_manifest
    assert first.run_manifest_receipt == second.run_manifest_receipt


def test_missing_pit_certification_fails_closed(tmp_path: Path):
    objects, evidence = _pass_graph()
    bad = dict(objects[0])
    bad["pit_certification_status"] = "UNVERIFIED"
    bad = seal_object(bad, "binding_hash")
    outcome = CanonicalSRFResearchWorkflow(tmp_path / "srf").validate_and_persist(
        [bad], evidence, evaluated_at=EVALUATED_AT
    )
    assert outcome.aggregate_outcome == "BLOCK"
    assert any(
        r["rule_id"] == "SRF-DAT-001"
        and r["outcome"] == "BLOCK"
        and r["error_code"] == "EQS-SRF-DATA-PIT-NOT-CERTIFIED"
        for r in outcome.bundle.results
    )


def test_missing_runtime_evidence_fails_closed(tmp_path: Path):
    objects, _ = _pass_graph()
    outcome = CanonicalSRFResearchWorkflow(tmp_path / "srf").validate_and_persist(
        objects, {}, evaluated_at=EVALUATED_AT
    )
    assert outcome.aggregate_outcome == "BLOCK"
    assert any(r["error_code"] == "EQS-SRF-UNKNOWN-REQUIRED-STATE" for r in outcome.bundle.results)


def test_content_addressed_tamper_is_detected(tmp_path: Path):
    objects, evidence = _pass_graph()
    workflow = CanonicalSRFResearchWorkflow(tmp_path / "srf")
    outcome = workflow.validate_and_persist(objects, evidence, evaluated_at=EVALUATED_AT)
    receipt = outcome.run_manifest["artifacts"]["canonical_objects"]
    path = workflow.store.root / receipt["relative_path"]
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    path.write_text("{}\n", encoding="utf-8")
    ok, failures = workflow.verify_store()
    assert not ok
    assert failures


def test_immutable_collision_refuses_different_bytes(tmp_path: Path):
    objects, evidence = _pass_graph()
    workflow = CanonicalSRFResearchWorkflow(tmp_path / "srf")
    outcome = workflow.validate_and_persist(objects, evidence, evaluated_at=EVALUATED_AT)
    receipt = outcome.run_manifest["artifacts"]["runtime_evidence"]
    path = workflow.store.root / receipt["relative_path"]
    path.chmod(stat.S_IREAD | stat.S_IWRITE)
    path.write_text('{"changed":true}\n', encoding="utf-8")
    with pytest.raises(ImmutableEvidenceError):
        workflow.validate_and_persist(objects, evidence, evaluated_at=EVALUATED_AT)


def test_research_handoff_assertion_does_not_authorize_paper_or_live(tmp_path: Path):
    objects, evidence = _pass_graph()
    workflow = CanonicalSRFResearchWorkflow(tmp_path / "srf")
    outcome = workflow.validate_and_persist(objects, evidence, evaluated_at=EVALUATED_AT)
    workflow.assert_valid_research_handoff(outcome)
    assert outcome.run_manifest["authority_boundary"]["paper_authorized"] is False
    assert outcome.run_manifest["authority_boundary"]["live_authorized"] is False


def test_blocked_run_cannot_be_asserted_as_valid_handoff(tmp_path: Path):
    objects, _ = _pass_graph()
    workflow = CanonicalSRFResearchWorkflow(tmp_path / "srf")
    outcome = workflow.validate_and_persist(objects, {}, evaluated_at=EVALUATED_AT)
    with pytest.raises(SRFIntegrationError):
        workflow.assert_valid_research_handoff(outcome)


def test_contract_hashes_match_sealed_v1():
    root = Path(__file__).parents[1] / "src" / "eqs_srf_validator" / "contracts"
    expected = {
        "eqs_srf_v1_schema_bundle.json": "b63a3bff190c4446c8fa080d74157e3e026fa4d54e758bd4129e55a043369e50",
        "eqs_srf_v1_validator_catalog.json": "4833a0770c04389dcb865f96e13e07d405d56121fd472054c80583607256229f",
        "eqs_srf_v1_error_codes.json": "b59d836baf964d2d529f98736ff944eb302f5aceb38e6573a73e8656e85a4712",
    }
    import hashlib
    for name, digest in expected.items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == digest
