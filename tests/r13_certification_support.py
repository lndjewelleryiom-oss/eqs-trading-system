from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

from quant_system.research.alpha_binding import (
    compute_binding_id,
    expected_final_decision,
)
from quant_system.research.r13_manifest import canonical_json
from test_alpha_data_binding_schema import _valid_report


def h(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def write_json(path: Path, payload: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json(payload)
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def write_bytes(path: Path, payload: bytes) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return sha256(payload).hexdigest()


def descriptor(root: Path, path: Path, kind: str, classification: str = "DERIVED_REAL_MARKET") -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "classification": classification,
        "artifact_type": kind,
    }


def create_genuine_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    parent = root / "parent_archive.bin"
    write_bytes(parent, b"deterministic-parent-archive")
    source_receipts = root / "source_receipt_index.json"
    write_json(source_receipts, {
        "schema_id": "EQS-SOURCE-RECEIPTS-v1",
        "source_class": "GENUINE",
        "receipt_count": 3,
        "all_hashes_verified": True,
    })
    validator_build = root / "validator_build.bin"
    write_bytes(validator_build, b"r13-validator-build-v1")

    protected = root / "protected_boundary.json"
    protected_sha = write_json(protected, {
        "broker_submission_enabled": False,
        "credentials_accessed": False,
        "live_capital_touched": False,
        "protected_files_changed": 0,
        "eqs06_options_included": False,
    })

    universe_fp = h("genuine-universe")
    partition = {
        "dataset_id": "alpha-v1-r13",
        "venue": "BINANCE_USDM",
        "instrument_id": "BTC-USDT-PERP:BINANCE_USDM",
        "event_date": "2026-09-30",
        "kind": "TRADE",
        "schema_version": "crypto-perps-v1",
        "row_count": 10,
        "content_hash": h("partition-content"),
        "relative_path": "dataset=alpha-v1-r13/venue=BINANCE_USDM/part-test.jsonl",
        "min_event_time": "2026-09-30T23:00:00+00:00",
        "max_event_time": "2026-09-30T23:59:00+00:00",
        "min_available_at": "2026-09-30T23:00:01+00:00",
        "max_available_at": "2026-09-30T23:59:01+00:00",
    }
    dataset_payload = {
        "dataset_id": "alpha-v1-r13",
        "format_version": "crypto-perps-research-v1",
        "decision_time": "2026-09-30T23:59:59+00:00",
        "start_time": "2026-09-30T23:00:00+00:00",
        "partitions": [partition],
        "universe_fingerprint": universe_fp,
        "event_count": 10,
        "event_identity_hash": h("event-identities"),
    }
    dataset_payload["manifest_fingerprint"] = sha256(canonical_json(dataset_payload)).hexdigest()
    dataset = root / "dataset_manifest.json"
    dataset_sha = write_json(dataset, dataset_payload)

    universe = root / "universe_snapshot.json"
    universe_sha = write_json(universe, {
        "schema_id": "EQS-H02-HISTORICAL-UNIVERSE-v1",
        "snapshot_id": "universe-genuine-1",
        "universe_fingerprint": universe_fp,
        "record_count": 1,
        "history_sha256": h("universe-history"),
        "point_in_time": True,
        "survivorship_free": True,
    })
    lifecycle = root / "lifecycle_event_set.json"
    lifecycle_sha = write_json(lifecycle, {
        "schema_id": "EQS-H02-LIFECYCLE-EVENTS-v1",
        "schema_version": "H02-LIFECYCLE-v1",
        "record_count": 4,
        "listing_events_present": True,
        "delisting_events_present": True,
        "spec_revision_events_present": True,
        "source_revision_events_present": True,
        "continuity_status": "PASS",
    })
    coverage = root / "coverage_record_set.json"
    coverage_sha = write_json(coverage, {
        "schema_id": "EQS-H02-COVERAGE-v1",
        "training_complete": True,
        "validation_complete": True,
        "locked_oos_complete": True,
        "records": [{
            "coverage_id": "coverage-1",
            "venue": "BINANCE_USDM",
            "instrument_id": "BTC-USDT-PERP:BINANCE_USDM",
            "series": "TRADES",
            "start_time": "2023-01-01T00:00:00Z",
            "end_time": "2026-09-30T23:59:59Z",
            "expected_intervals": 10,
            "observed_intervals": 10,
            "missing_intervals": 0,
            "partial_intervals": 0,
            "availability_status": "COMPLETE",
            "record_sha256": h("coverage-record"),
        }],
    })
    eligibility = root / "eligibility_record_set.json"
    eligibility_sha = write_json(eligibility, {
        "schema_id": "EQS-H02-ELIGIBILITY-v1",
        "record_count": 1,
        "records": [{
            "eligibility_id": "eligibility-1",
            "instrument_id": "BTC-USDT-PERP:BINANCE_USDM",
            "venue": "BINANCE_USDM",
            "effective_from": "2023-01-01T00:00:00Z",
            "effective_to": None,
            "eligible": True,
            "reason_codes": ["ACTIVE_PIT"],
            "source_universe_fingerprint": universe_fp,
            "record_sha256": h("eligibility-record"),
        }],
    })
    pit = root / "pit_certification.json"
    pit_sha = write_json(pit, {
        "schema_id": "EQS-R1.3-PIT-CERTIFICATION-v1",
        "source_class": "GENUINE",
        "status": "PASS",
        "certification_id": "pit-genuine-1",
        "audit_version": "pit-v1",
        "audit_generated_at": "2026-10-01T07:30:00Z",
        "records_checked": 10,
        "violations": 0,
        "future_events_detected": 0,
        "future_revisions_detected": 0,
        "survivorship_bias_detected": False,
        "backward_availability_adjustments": 0,
        "decision_boundary_crossings": 0,
        "time_basis": {
            "timezone": "UTC",
            "event_time_semantics": "AS_KNOWN_AT",
            "publication_time_required": True,
            "effective_time_required": True,
            "available_at_required": True,
            "ingestion_time_recorded": True,
            "revision_time_recorded": True,
            "future_visibility_prohibited": True,
        },
    })

    rr_catalogue = root / "rr_catalogue.json"
    rr_catalogue_sha = write_json(rr_catalogue, {
        "catalogue_id": "EQS-H02-RR-CATALOGUE",
        "catalogue_version": "RR-001-030-v1",
        "rules": [{"rule_id": f"RR-{i:03d}"} for i in range(1, 31)],
    })
    rr_results = root / "rr_results.json"
    rr_results_sha = write_json(rr_results, {
        "schema_id": "EQS-H02-RR-RESULTS-v1",
        "catalogue_sha256": rr_catalogue_sha,
        "validator_id": "EQS-H02-R1.3-VALIDATOR",
        "validator_version": "v1.0.0",
        "validator_build_sha256": sha256(validator_build.read_bytes()).hexdigest(),
        "semantic_engine_version": "r13-semantic-v1",
        "run_id": "rr-run-genuine-1",
        "run_started_at": "2026-10-01T08:00:00Z",
        "run_completed_at": "2026-10-01T08:30:00Z",
        "result": "PASS",
        "results": [
            {"rule_id": f"RR-{i:03d}", "status": "PASS"}
            for i in range(1, 31)
        ],
    })

    alpha = deepcopy(_valid_report())
    alpha["generated_at"] = "2026-10-01T08:45:00+00:00"
    alpha["parent"]["archive_sha256"] = sha256(parent.read_bytes()).hexdigest()
    alpha["parent"]["dataset_builder_version"] = "r1.3-builder-v1"
    alpha["dataset"].update({
        "dataset_id": dataset_payload["dataset_id"],
        "manifest_fingerprint": dataset_payload["manifest_fingerprint"],
        "universe_fingerprint": universe_fp,
        "event_identity_hash": dataset_payload["event_identity_hash"],
        "replay_fingerprint": h("replay"),
        "decision_time": dataset_payload["decision_time"],
        "partition_count": 1,
        "event_count": 10,
        "raw_source_count": 3,
        "venues": ["BINANCE_USDM"],
        "coverage": {"training": True, "validation": True, "locked_oos": True},
    })
    alpha["feature_engine"].update({
        "dataset_manifest_fingerprint": dataset_payload["manifest_fingerprint"],
        "universe_fingerprint": universe_fp,
        "max_decision_time": dataset_payload["decision_time"],
        "run_manifest_fingerprints": [h("feature-run-genuine")],
    })
    alpha["protected_boundary"]["evidence_sha256"] = protected_sha
    for gate_id, gate in alpha["gates"].items():
        gate_file = root / "gate_evidence" / f"{gate_id}.json"
        gate_sha = write_json(gate_file, {
            "gate_id": gate_id,
            "source_class": "GENUINE",
            "status": "PASS",
        })
        gate["evidence"] = [{
            "evidence_type": "HASH_REPORT",
            "uri_or_path": gate_file.relative_to(root).as_posix(),
            "sha256": gate_sha,
            "classification": "DERIVED_REAL_MARKET",
            "description": f"{gate_id} genuine gate evidence",
        }]
    alpha["binding_id"] = compute_binding_id(alpha)
    alpha["final_decision"] = expected_final_decision(alpha)
    alpha_file = root / "alpha_binding.json"
    write_json(alpha_file, alpha)

    dataset_content_hash = sha256(canonical_json([partition["content_hash"]])).hexdigest()
    partition_index_hash = sha256(canonical_json([partition])).hexdigest()
    h02 = root / "h02_batch_manifest.json"
    h02_payload = {
        "schema_id": "EQS-H02-BATCH-MANIFEST-v1",
        "source_class": "GENUINE",
        "status": "PASS",
        "dataset_immutable": True,
        "dataset_version": "R1.3-genuine-test",
        "builder_version": "r1.3-builder-v1",
        "feature_definition_hash": h("feature-definition"),
        "dataset_content_hash": dataset_content_hash,
        "partition_index_hash": partition_index_hash,
        "replay_fingerprint": h("replay"),
        "coverage_start": "2023-01-01T00:00:00Z",
        "coverage_end": "2026-09-30T23:59:59Z",
        "bindings": {
            "dataset_manifest_sha256": dataset_sha,
            "universe_snapshot_sha256": universe_sha,
            "lifecycle_event_set_sha256": lifecycle_sha,
            "coverage_record_set_sha256": coverage_sha,
            "eligibility_record_set_sha256": eligibility_sha,
            "pit_certification_sha256": pit_sha,
            "source_receipt_index_sha256": sha256(source_receipts.read_bytes()).hexdigest(),
            "parent_archive_sha256": sha256(parent.read_bytes()).hexdigest(),
            "protected_boundary_report_sha256": protected_sha,
        },
    }
    write_json(h02, h02_payload)

    role_data = {
        "h02_batch_manifest": (h02, "OTHER"),
        "dataset_manifest": (dataset, "DATASET_MANIFEST"),
        "universe_snapshot": (universe, "UNIVERSE_SNAPSHOT"),
        "lifecycle_event_set": (lifecycle, "LIFECYCLE_EVENT_SET"),
        "coverage_record_set": (coverage, "COVERAGE_RECORD_SET"),
        "eligibility_record_set": (eligibility, "ELIGIBILITY_RECORD_SET"),
        "pit_certification": (pit, "PIT_AUDIT"),
        "rr_catalogue": (rr_catalogue, "RULE_CATALOGUE"),
        "rr_results": (rr_results, "VALIDATOR_REPORT"),
        "alpha_binding": (alpha_file, "OTHER"),
        "source_receipt_index": (source_receipts, "RAW_RECEIPT_INDEX"),
        "parent_archive": (parent, "OTHER"),
        "validator_build": (validator_build, "VALIDATOR_REPORT"),
        "protected_boundary_report": (protected, "PROTECTED_BOUNDARY_REPORT"),
    }
    artifacts = {
        role: descriptor(
            root,
            path,
            kind,
            "REAL_MARKET" if role == "source_receipt_index" else "DERIVED_REAL_MARKET",
        )
        for role, (path, kind) in role_data.items()
    }
    index = {
        "schema_id": "EQS-R1.3-GENUINE-CERTIFICATION-INPUTS-v1",
        "manifest_id": "r13m_genuine_test_001",
        "manifest_generation": 1,
        "created_at": "2026-10-01T09:00:00Z",
        "artifacts": artifacts,
    }
    index_path = root / "certification_inputs.json"
    write_json(index_path, index)
    return index_path


def load_index(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def rewrite_index(path: Path, payload: dict[str, object]) -> None:
    write_json(path, payload)


def update_descriptor_hash(index_path: Path, role: str) -> None:
    index = load_index(index_path)
    descriptor_obj = index["artifacts"][role]
    artifact_path = index_path.parent / descriptor_obj["path"]
    descriptor_obj["sha256"] = sha256(artifact_path.read_bytes()).hexdigest()
    rewrite_index(index_path, index)
