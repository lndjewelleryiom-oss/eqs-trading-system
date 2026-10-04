from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from quant_system.research.r13_manifest import manifest_content_hash


def h(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _artifact(root: Path, name: str, artifact_type: str, content: bytes) -> tuple[dict[str, object], str]:
    path = root / name
    path.write_bytes(content)
    digest = sha256(content).hexdigest()
    return ({
        "artifact_id": name,
        "artifact_type": artifact_type,
        "path_or_uri": name,
        "sha256": digest,
        "byte_size": len(content),
        "classification": "DERIVED_REAL_MARKET",
    }, digest)


def refresh_manifest_hash(manifest: dict[str, object]) -> None:
    manifest["manifest_hash"] = manifest_content_hash(manifest)


def build_valid_manifest(root: Path, *, generation: int = 1, manifest_id: str = "r13m_test0001") -> dict[str, object]:
    specs = [
        ("h02.json", "OTHER", b"h02-batch"),
        ("universe.json", "UNIVERSE_SNAPSHOT", b"universe-snapshot"),
        ("lifecycle.json", "LIFECYCLE_EVENT_SET", b"lifecycle-events"),
        ("coverage.json", "COVERAGE_RECORD_SET", b"coverage-records"),
        ("eligibility.json", "ELIGIBILITY_RECORD_SET", b"eligibility-records"),
        ("validator.bin", "VALIDATOR_REPORT", b"validator-build"),
        ("rules.json", "RULE_CATALOGUE", b"rr-catalog"),
        ("pit.json", "PIT_AUDIT", b"pit-certification"),
        ("receipts.json", "RAW_RECEIPT_INDEX", b"source-receipts"),
        ("parent.bin", "OTHER", b"parent-archive"),
    ]
    artifacts = []
    hashes = {}
    for name, kind, content in specs:
        artifact, digest = _artifact(root, name, kind, content)
        artifacts.append(artifact)
        hashes[name] = digest

    universe_fp = h("universe-fingerprint")
    dataset_fp = h("dataset-manifest")
    manifest: dict[str, object] = {
        "schema_id": "EQS-R1.3-HISTORICAL-CERTIFICATION-MANIFEST-v1",
        "manifest_id": manifest_id,
        "manifest_generation": generation,
        "created_at": "2026-10-01T07:00:00Z",
        "source_class": "GENUINE",
        "environment": "RESEARCH",
        "dataset": {
            "dataset_id": "EQS-HIST-MULTIASSET-R1.3",
            "dataset_version": f"R1.3-test-g{generation}",
            "format_version": "crypto-perps-research-v1",
            "builder_version": "r1.3-builder-v1",
            "dataset_manifest_fingerprint": dataset_fp,
            "dataset_content_hash": h("dataset-content"),
            "event_identity_hash": h("events"),
            "replay_fingerprint": h("replay"),
            "partition_index_hash": h("partitions"),
            "partition_count": 3,
            "event_count": 100,
            "coverage_start": "2023-01-01T00:00:00Z",
            "coverage_end": "2026-09-30T23:59:59Z",
            "decision_time": "2026-09-30T23:59:59Z",
            "venues": ["BINANCE_USDM", "BYBIT_LINEAR", "OKX_SWAP"],
            "instrument_type": "LINEAR_PERPETUAL",
        },
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
        "pit": {
            "status": "PASS",
            "certification_id": "pit-test-1",
            "certification_sha256": hashes["pit.json"],
            "audit_version": "pit-v1",
            "audit_generated_at": "2026-10-01T06:30:00Z",
            "records_checked": 100,
            "violations": 0,
            "future_events_detected": 0,
            "future_revisions_detected": 0,
            "survivorship_bias_detected": False,
            "backward_availability_adjustments": 0,
            "decision_boundary_crossings": 0,
        },
        "universe": {
            "schema_id": "EQS-H02-HISTORICAL-UNIVERSE-v1",
            "snapshot_id": "universe-test-1",
            "snapshot_sha256": hashes["universe.json"],
            "universe_fingerprint": universe_fp,
            "record_count": 3,
            "history_sha256": h("universe-history"),
            "point_in_time": True,
            "survivorship_free": True,
        },
        "lifecycle": {
            "schema_id": "EQS-H02-LIFECYCLE-EVENTS-v1",
            "schema_version": "H02-LIFECYCLE-v1",
            "record_count": 4,
            "event_set_sha256": hashes["lifecycle.json"],
            "listing_events_present": True,
            "delisting_events_present": True,
            "spec_revision_events_present": True,
            "source_revision_events_present": True,
            "continuity_status": "PASS",
        },
        "coverage": {
            "schema_id": "EQS-H02-COVERAGE-v1",
            "coverage_record_set_sha256": hashes["coverage.json"],
            "training_complete": True,
            "validation_complete": True,
            "locked_oos_complete": True,
            "records": [{
                "coverage_id": "coverage-1",
                "venue": "BINANCE_USDM",
                "instrument_id": "BTCUSDT",
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
        },
        "eligibility": {
            "schema_id": "EQS-H02-ELIGIBILITY-v1",
            "eligibility_record_set_sha256": hashes["eligibility.json"],
            "record_count": 1,
            "records": [{
                "eligibility_id": "eligibility-1",
                "instrument_id": "BTCUSDT",
                "venue": "BINANCE_USDM",
                "effective_from": "2023-01-01T00:00:00Z",
                "effective_to": None,
                "eligible": True,
                "reason_codes": ["ACTIVE_PIT"],
                "source_universe_fingerprint": universe_fp,
                "record_sha256": h("eligibility-record"),
            }],
        },
        "feature_binding": {
            "feature_engine_version": "crypto-perps-feature-engine-v1",
            "dataset_manifest_fingerprint": dataset_fp,
            "universe_fingerprint": universe_fp,
            "feature_run_manifest_fingerprints": [h("feature-run")],
            "feature_definition_hash": h("feature-definition"),
        },
        "validator": {
            "validator_id": "EQS-H02-R1.3-VALIDATOR",
            "validator_version": "v1.0.0",
            "validator_build_sha256": hashes["validator.bin"],
            "semantic_engine_version": "r13-semantic-v1",
            "json_schema_version": "2020-12",
            "run_id": "validator-run-1",
            "run_started_at": "2026-10-01T06:00:00Z",
            "run_completed_at": "2026-10-01T06:45:00Z",
            "result": "PASS",
        },
        "rule_catalogue": {
            "catalogue_id": "EQS-H02-RR-CATALOGUE",
            "catalogue_version": "RR-001-030-v1",
            "catalogue_sha256": hashes["rules.json"],
            "first_rule": "RR-001",
            "last_rule": "RR-030",
            "rule_count": 30,
            "passed_rule_count": 30,
            "failed_rule_count": 0,
            "blocked_rule_count": 0,
        },
        "certification": {
            "certification_run_id": "cert-run-1",
            "status": "PASSED",
            "a01_a28": {f"A{i:02d}": "PASS" for i in range(1, 29)},
            "all_hard_gates_pass": True,
            "locked_oos_seal_intact": True,
            "dataset_immutable": True,
            "manifest_immutable": True,
        },
        "artifact_inventory": artifacts,
        "provenance": {
            "h02_batch_manifest_sha256": hashes["h02.json"],
            "universe_snapshot_sha256": hashes["universe.json"],
            "lifecycle_event_schema_version": "H02-LIFECYCLE-v1",
            "lifecycle_event_set_sha256": hashes["lifecycle.json"],
            "coverage_record_set_sha256": hashes["coverage.json"],
            "eligibility_record_set_sha256": hashes["eligibility.json"],
            "validator_version": "v1.0.0",
            "validator_build_sha256": hashes["validator.bin"],
            "rule_catalog_version": "RR-001-030-v1",
            "rule_catalog_sha256": hashes["rules.json"],
            "pit_certification_sha256": hashes["pit.json"],
            "source_receipt_index_sha256": hashes["receipts.json"],
            "parent_archive_sha256": hashes["parent.bin"],
        },
        "protected_boundary": {
            "broker_submission_enabled": False,
            "credentials_accessed": False,
            "live_capital_touched": False,
            "protected_files_changed": 0,
            "eqs06_options_included": False,
            "boundary_report_sha256": h("boundary-report"),
        },
        "manifest_hash": "0" * 64,
    }
    refresh_manifest_hash(manifest)
    return manifest
