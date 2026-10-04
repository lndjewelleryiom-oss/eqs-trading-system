from __future__ import annotations

from copy import deepcopy
from hashlib import sha256

import pytest

from quant_system.research.r13_admission import R13AdmissionError, R13AdmissionLedger
from quant_system.research.r13_manifest import (
    R13ManifestSchemaError,
    R13SemanticRuleError,
    SEMANTIC_RULE_IDS,
    validate_manifest_schema,
    validate_r13_semantics,
)
from r13_manifest_support import build_valid_manifest, h, refresh_manifest_hash


def test_schema_accepts_canonical_valid_manifest(tmp_path):
    manifest = build_valid_manifest(tmp_path)
    validate_manifest_schema(manifest)


def test_schema_requires_all_canonical_hash_fields(tmp_path):
    manifest = build_valid_manifest(tmp_path)
    del manifest["dataset"]["dataset_content_hash"]
    with pytest.raises(R13ManifestSchemaError):
        validate_manifest_schema(manifest)


def _mutate(rule_id: str, manifest: dict[str, object], root) -> None:
    dataset = manifest["dataset"]
    feature = manifest["feature_binding"]
    universe = manifest["universe"]
    lifecycle = manifest["lifecycle"]
    coverage = manifest["coverage"]
    eligibility = manifest["eligibility"]
    validator = manifest["validator"]
    catalogue = manifest["rule_catalogue"]
    provenance = manifest["provenance"]
    pit = manifest["pit"]

    if rule_id == "R13-M001":
        dataset["coverage_start"] = "2026-10-01T00:00:00Z"
    elif rule_id == "R13-M002":
        dataset["decision_time"] = "2026-09-01T00:00:00Z"
    elif rule_id == "R13-M003":
        validator["run_completed_at"] = "2026-10-01T08:00:00Z"
    elif rule_id == "R13-M004":
        feature["dataset_manifest_fingerprint"] = h("wrong-dataset")
    elif rule_id == "R13-M005":
        feature["universe_fingerprint"] = h("wrong-universe")
    elif rule_id == "R13-M006":
        provenance["universe_snapshot_sha256"] = h("wrong-snapshot")
    elif rule_id == "R13-M007":
        provenance["lifecycle_event_set_sha256"] = h("wrong-lifecycle")
    elif rule_id == "R13-M008":
        provenance["coverage_record_set_sha256"] = h("wrong-coverage")
    elif rule_id == "R13-M009":
        provenance["eligibility_record_set_sha256"] = h("wrong-eligibility")
    elif rule_id == "R13-M010":
        provenance["validator_version"] = "v1.0.1"
    elif rule_id == "R13-M011":
        provenance["validator_build_sha256"] = h("wrong-validator")
    elif rule_id == "R13-M012":
        provenance["rule_catalog_version"] = "RR-001-030-v2"
    elif rule_id == "R13-M013":
        provenance["rule_catalog_sha256"] = h("wrong-catalog")
    elif rule_id == "R13-M014":
        provenance["pit_certification_sha256"] = h("wrong-pit")
    elif rule_id == "R13-M015":
        eligibility["record_count"] = 2
    elif rule_id == "R13-M016":
        coverage["records"][0]["observed_intervals"] = 11
    elif rule_id == "R13-M017":
        coverage["records"][0]["partial_intervals"] = 1
    elif rule_id == "R13-M018":
        eligibility["records"][0]["effective_to"] = "2022-12-31T00:00:00Z"
    elif rule_id == "R13-M019":
        eligibility["records"][0]["source_universe_fingerprint"] = h("wrong-universe")
    elif rule_id == "R13-M020":
        provenance["source_receipt_index_sha256"] = h("not-in-inventory")
    elif rule_id == "R13-M021":
        bad = root / "extra.bin"
        bad.write_bytes(b"actual")
        manifest["artifact_inventory"].append({
            "artifact_id": "bad-extra",
            "artifact_type": "OTHER",
            "path_or_uri": "extra.bin",
            "sha256": sha256(b"different").hexdigest(),
            "byte_size": len(b"actual"),
            "classification": "DERIVED_REAL_MARKET",
        })
    elif rule_id == "R13-M022":
        manifest["manifest_hash"] = "0" * 64
        return
    else:
        raise AssertionError(rule_id)
    refresh_manifest_hash(manifest)


@pytest.mark.parametrize("rule_id", SEMANTIC_RULE_IDS[:22])
def test_semantic_rules_m001_through_m022_fail_closed(rule_id, tmp_path):
    manifest = build_valid_manifest(tmp_path)
    _mutate(rule_id, manifest, tmp_path)
    with pytest.raises(R13SemanticRuleError) as exc:
        validate_r13_semantics(manifest, artifact_root=tmp_path)
    assert exc.value.rule_id == rule_id


def test_all_m001_through_m024_pass_for_valid_manifest_without_history(tmp_path):
    manifest = build_valid_manifest(tmp_path)
    result = validate_r13_semantics(manifest, artifact_root=tmp_path)
    assert result.passed_rules == SEMANTIC_RULE_IDS


def test_m023_rejects_non_monotonic_lineage_generation_in_admit(tmp_path):
    ledger = R13AdmissionLedger(tmp_path / "r13.sqlite")
    first = build_valid_manifest(tmp_path, generation=1, manifest_id="r13m_lineage01")
    ledger.admit(first)

    second = deepcopy(first)
    second["manifest_id"] = "r13m_lineage02"
    second["dataset"]["dataset_version"] = "R1.3-other"
    refresh_manifest_hash(second)
    with pytest.raises(R13AdmissionError) as exc:
        ledger.admit(second)
    assert exc.value.code == "R13_M023_FAILED"
    ledger.close()


def test_m024_rejects_manifest_id_rebinding_in_admit(tmp_path):
    ledger = R13AdmissionLedger(tmp_path / "r13.sqlite")
    first = build_valid_manifest(tmp_path, generation=1, manifest_id="r13m_binding01")
    ledger.admit(first)

    second = deepcopy(first)
    second["manifest_generation"] = 2
    second["dataset"]["dataset_version"] = "R1.3-other"
    refresh_manifest_hash(second)
    with pytest.raises(R13AdmissionError) as exc:
        ledger.admit(second)
    assert exc.value.code == "R13_M024_FAILED"
    ledger.close()


def test_admit_rejects_schema_failure_before_write(tmp_path):
    ledger = R13AdmissionLedger(tmp_path / "r13.sqlite")
    manifest = build_valid_manifest(tmp_path)
    manifest["source_class"] = "SYNTHETIC"
    refresh_manifest_hash(manifest)
    with pytest.raises(R13AdmissionError) as exc:
        ledger.admit(manifest)
    assert exc.value.code == "R1_3_MANIFEST_INVALID"
    assert ledger.db.execute("SELECT COUNT(*) FROM r13_admissions").fetchone()[0] == 0
    ledger.close()


def test_admit_persists_only_after_all_24_rules_pass(tmp_path):
    ledger = R13AdmissionLedger(tmp_path / "r13.sqlite")
    manifest = build_valid_manifest(tmp_path)
    record = ledger.admit(manifest)
    assert record.manifest.manifest_sha256 == manifest["manifest_hash"]
    assert ledger.db.execute("SELECT COUNT(*) FROM r13_admissions").fetchone()[0] == 1
    assert ledger.verify_hash_chain()
    ledger.close()
