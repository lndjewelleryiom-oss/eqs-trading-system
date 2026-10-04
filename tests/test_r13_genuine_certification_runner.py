from __future__ import annotations

from hashlib import sha256
import json

import pytest

from quant_system.research.r13_admission import R13AdmissionLedger
from quant_system.research.r13_certification import (
    GenuineR13CertificationError,
    GenuineR13CertificationRunner,
)
from quant_system.research.r13_manifest import validate_manifest_schema
from r13_certification_support import (
    create_genuine_bundle,
    load_index,
    rewrite_index,
    update_descriptor_hash,
)


def test_complete_genuine_bundle_builds_and_admits_canonical_manifest(tmp_path):
    bundle = tmp_path / "bundle"
    index_path = create_genuine_bundle(bundle)
    output = tmp_path / "out" / "manifest.json"
    ledger = R13AdmissionLedger(tmp_path / "r13.sqlite")

    result = GenuineR13CertificationRunner(project_root=tmp_path).run(
        index_path,
        output,
        admission_ledger=ledger,
        admit=True,
    )

    assert output.exists()
    assert output.with_suffix(".json.sha256").exists()
    assert result.admission is not None
    assert result.admission.generation == 1
    assert result.manifest["source_class"] == "GENUINE"
    assert result.manifest["certification"]["status"] == "PASSED"
    assert all(
        value == "PASS"
        for value in result.manifest["certification"]["a01_a28"].values()
    )
    validate_manifest_schema(result.manifest)
    assert ledger.active().manifest.manifest_sha256 == result.manifest["manifest_hash"]
    assert ledger.verify_hash_chain()
    ledger.close()


def test_missing_required_artifact_fails_before_output_or_admission(tmp_path):
    bundle = tmp_path / "bundle"
    index_path = create_genuine_bundle(bundle)
    (bundle / "pit_certification.json").unlink()
    output = tmp_path / "out" / "manifest.json"
    ledger = R13AdmissionLedger(tmp_path / "r13.sqlite")

    with pytest.raises(GenuineR13CertificationError) as exc:
        GenuineR13CertificationRunner(project_root=tmp_path).run(
            index_path,
            output,
            admission_ledger=ledger,
            admit=True,
        )

    assert exc.value.code == "R13_CERT_REQUIRED_ARTIFACT_MISSING"
    assert not output.exists()
    assert ledger.db.execute("SELECT COUNT(*) FROM r13_admissions").fetchone()[0] == 0
    ledger.close()


def test_tampered_required_artifact_fails_hash_verification(tmp_path):
    bundle = tmp_path / "bundle"
    index_path = create_genuine_bundle(bundle)
    rr = bundle / "rr_results.json"
    rr.write_bytes(rr.read_bytes() + b"\n")
    output = tmp_path / "manifest.json"

    with pytest.raises(GenuineR13CertificationError) as exc:
        GenuineR13CertificationRunner(project_root=tmp_path).run(index_path, output)

    assert exc.value.code == "R13_CERT_ARTIFACT_HASH_MISMATCH"
    assert not output.exists()


def test_a01_a28_referenced_evidence_bytes_are_verified(tmp_path):
    bundle = tmp_path / "bundle"
    index_path = create_genuine_bundle(bundle)
    evidence = bundle / "gate_evidence" / "A12.json"
    evidence.write_bytes(evidence.read_bytes() + b"tamper")
    output = tmp_path / "manifest.json"

    with pytest.raises(GenuineR13CertificationError) as exc:
        GenuineR13CertificationRunner(project_root=tmp_path).run(index_path, output)

    assert exc.value.code == "R13_CERT_GATE_EVIDENCE_HASH_MISMATCH"
    assert not output.exists()


def test_rr_rule_failure_is_rejected_even_when_artifact_hash_is_updated(tmp_path):
    bundle = tmp_path / "bundle"
    index_path = create_genuine_bundle(bundle)
    rr_path = bundle / "rr_results.json"
    rr = json.loads(rr_path.read_text(encoding="utf-8"))
    rr["results"][17]["status"] = "FAIL"
    rr_path.write_text(
        json.dumps(rr, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    update_descriptor_hash(index_path, "rr_results")
    output = tmp_path / "manifest.json"

    with pytest.raises(GenuineR13CertificationError) as exc:
        GenuineR13CertificationRunner(project_root=tmp_path).run(index_path, output)

    assert exc.value.code == "R13_CERT_RR_INVALID"
    assert not output.exists()


def test_test_fixture_classification_is_rejected_at_input_boundary(tmp_path):
    bundle = tmp_path / "bundle"
    index_path = create_genuine_bundle(bundle)
    index = load_index(index_path)
    index["artifacts"]["h02_batch_manifest"]["classification"] = "TEST_FIXTURE"
    rewrite_index(index_path, index)
    output = tmp_path / "manifest.json"

    with pytest.raises(GenuineR13CertificationError) as exc:
        GenuineR13CertificationRunner(project_root=tmp_path).run(index_path, output)

    assert exc.value.code == "R13_CERT_INPUT_SCHEMA_INVALID"
    assert not output.exists()


def test_parent_archive_cross_binding_is_fail_closed(tmp_path):
    bundle = tmp_path / "bundle"
    index_path = create_genuine_bundle(bundle)
    alpha_path = bundle / "alpha_binding.json"
    alpha = json.loads(alpha_path.read_text(encoding="utf-8"))
    alpha["parent"]["archive_sha256"] = "0" * 64

    from quant_system.research.alpha_binding import compute_binding_id, expected_final_decision

    alpha["binding_id"] = compute_binding_id(alpha)
    alpha["final_decision"] = expected_final_decision(alpha)
    alpha_path.write_text(
        json.dumps(alpha, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    update_descriptor_hash(index_path, "alpha_binding")
    output = tmp_path / "manifest.json"

    with pytest.raises(GenuineR13CertificationError) as exc:
        GenuineR13CertificationRunner(project_root=tmp_path).run(index_path, output)

    assert exc.value.code == "R13_CERT_CROSS_BINDING_MISMATCH"
    assert not output.exists()


def test_real_current_tree_remains_unadmitted_without_full_certification_bundle(tmp_path):
    ledger = R13AdmissionLedger(tmp_path / "r13.sqlite")
    with pytest.raises(Exception):
        ledger.active()
    assert ledger.db.execute("SELECT COUNT(*) FROM r13_admissions").fetchone()[0] == 0
    ledger.close()
