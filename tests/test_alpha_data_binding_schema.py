from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quant_system.research.alpha_binding import (
    AlphaDataBindingError,
    CAMPAIGN_GATES,
    FAILURE_CODES,
    GATE_IDS,
    GLOBAL_HARD_GATES,
    SCHEMA_VERSION,
    compute_binding_id,
    compute_campaign_set_fingerprint,
    expected_final_decision,
    frozen_campaign_contracts,
    validate_alpha_data_binding_report,
)


def _h(value: str) -> str:
    return sha256(value.encode()).hexdigest()


def _evidence(tag: str) -> dict[str, object]:
    return {
        "evidence_type": "TEST_LOG",
        "uri_or_path": f"artifacts/alpha-binding/{tag}.json",
        "sha256": _h(tag),
        "classification": "DERIVED_REAL_MARKET",
        "description": tag,
    }


def _gate(gate_id: str) -> dict[str, object]:
    scope = "GLOBAL"
    severity = "HARD_BLOCK"
    if gate_id in {"A19", "A20"}:
        scope, severity = "INSTRUMENT_FILTER", "FILTER"
    elif gate_id in {"A21", "A22", "A23"}:
        scope, severity = "CAMPAIGN", "CAMPAIGN_BLOCK"
    elif gate_id == "A26":
        scope, severity = "CAMPAIGN", "CAMPAIGN_INVALIDATION"
    return {
        "gate_id": gate_id,
        "name": f"Gate {gate_id}",
        "scope": scope,
        "severity": severity,
        "status": "PASS",
        "pass_condition": f"{gate_id} frozen pass condition",
        "observed": {"verified": True},
        "evidence": [_evidence(gate_id)],
        "failure_codes": [],
    }


def _valid_report() -> dict[str, object]:
    contracts = frozen_campaign_contracts()
    campaign_identities = [
        {
            "campaign_id": campaign_id,
            "campaign_fingerprint": contract["campaign_fingerprint"],
            "preregistration_path": f"research/preregistrations/v1/{campaign_id}.json",
        }
        for campaign_id, contract in contracts.items()
    ]
    readiness = []
    for campaign_id, contract in contracts.items():
        readiness.append({
            "campaign_id": campaign_id,
            "campaign_fingerprint": contract["campaign_fingerprint"],
            "eligibility_mask_fingerprint": _h(f"mask:{campaign_id}"),
            "eligible_instrument_count": 12,
            "eligible_observations": 10000,
            "excluded_instrument_times": {"A19": 10, "A20": 20, "A19_A20": 2},
            "independent_samples": contract["minimum_independent_samples"],
            "minimum_independent_samples": contract["minimum_independent_samples"],
            "minimum_candidate_trades": contract["minimum_candidate_trades"],
            "candidate_trade_count_status": "DEFERRED_UNTIL_CANDIDATE_EXECUTION",
            "walk_forward_fold_count": 6,
            "minimum_walk_forward_folds": 6,
            "feature_coverage_complete": True,
            "training_validation_ready": True,
            "locked_oos_status": "SEALED",
            "gate_statuses": {
                gate_id: {"status": "PASS", "failure_codes": []}
                for gate_id in CAMPAIGN_GATES
            },
            "binding_ready": True,
            "block_reason_codes": [],
        })
    manifest = _h("manifest")
    universe = _h("universe")
    report: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "binding_id": "0" * 64,
        "generated_at": "2026-09-22T22:45:00+00:00",
        "evidence_classification": "REAL_MARKET",
        "parent": {
            "archive_path": "/Quant trading/EQS/master.zip",
            "archive_sha256": _h("parent"),
            "code_version": "master-test",
            "dataset_builder_version": "r1.3",
            "build_command_fingerprint": _h("build"),
        },
        "dataset": {
            "format_version": "crypto-perps-research-v1",
            "dataset_id": "alpha-v1-r13",
            "manifest_fingerprint": manifest,
            "universe_fingerprint": universe,
            "event_identity_hash": _h("events"),
            "replay_fingerprint": _h("replay"),
            "decision_time": "2026-09-21T23:59:59+00:00",
            "partition_count": 100,
            "event_count": 100000,
            "raw_source_count": 500,
            "venues": ["BINANCE_USDM", "BYBIT_LINEAR", "OKX_SWAP"],
            "coverage": {
                "training": True,
                "validation": True,
                "locked_oos": True,
            },
        },
        "feature_engine": {
            "version": "crypto-perps-feature-engine-v1",
            "run_manifest_fingerprints": [_h("feature-run")],
            "dataset_manifest_fingerprint": manifest,
            "universe_fingerprint": universe,
            "max_decision_time": "2026-09-21T23:59:59+00:00",
        },
        "alpha_campaign_set": {
            "version": "alpha-campaign-v1",
            "campaigns": campaign_identities,
            "campaign_set_fingerprint": compute_campaign_set_fingerprint(campaign_identities),
        },
        "gates": {gate_id: _gate(gate_id) for gate_id in GATE_IDS},
        "campaign_readiness": readiness,
        "protected_boundary": {
            "r1_2_unchanged": True,
            "f7_unchanged": True,
            "tracker_unchanged": True,
            "broker_submission_enabled": False,
            "credentials_accessed": False,
            "live_capital_touched": False,
            "protected_files_compared": 35,
            "protected_files_changed": 0,
            "evidence_sha256": _h("protected"),
        },
        "final_decision": {},
    }
    report["binding_id"] = compute_binding_id(report)
    report["final_decision"] = expected_final_decision(report)
    return report


def _refresh(report: dict[str, object]) -> None:
    report["binding_id"] = compute_binding_id(report)
    report["final_decision"] = expected_final_decision(report)


def test_versioned_json_schema_declares_all_a01_a28_and_final_gate() -> None:
    schema = json.loads(Path("schemas/alpha_data_binding_gates_v1.schema.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert schema["properties"]["gates"]["required"] == list(GATE_IDS)
    assert schema["$defs"]["final"]["properties"]["ALPHA_DATA_BINDING_READY"]["type"] == "boolean"
    assert schema["$defs"]["campaignReadiness"]["properties"]["candidate_trade_count_status"]["const"] == "DEFERRED_UNTIL_CANDIDATE_EXECUTION"


def test_valid_all_pass_report_is_binding_ready_and_validates() -> None:
    report = _valid_report()
    validate_alpha_data_binding_report(report)
    assert report["final_decision"]["ALPHA_DATA_BINDING_READY"] is True
    assert report["final_decision"]["locked_oos_may_be_opened"] is True
    assert len(report["final_decision"]["campaigns_ready"]) == 6


def test_missing_gate_fails_closed() -> None:
    report = _valid_report()
    del report["gates"]["A12"]
    with pytest.raises(AlphaDataBindingError, match="exactly A01 through A28"):
        validate_alpha_data_binding_report(report)


def test_unknown_failure_code_is_rejected() -> None:
    report = _valid_report()
    report["gates"]["A05"]["status"] = "FAIL"
    report["gates"]["A05"]["failure_codes"] = ["A05_MADE_UP_CODE"]
    with pytest.raises(AlphaDataBindingError, match="unknown failure"):
        validate_alpha_data_binding_report(report)


def test_global_blocked_gate_forces_every_campaign_not_ready_and_final_false() -> None:
    report = _valid_report()
    report["gates"]["A18"]["status"] = "BLOCKED"
    report["gates"]["A18"]["failure_codes"] = ["A18_TRAINING_WINDOW_INCOMPLETE"]
    report["gates"]["A18"]["evidence"] = []
    for item in report["campaign_readiness"]:
        item["binding_ready"] = False
        item["block_reason_codes"] = ["A18_TRAINING_WINDOW_INCOMPLETE"]
    _refresh(report)
    validate_alpha_data_binding_report(report)
    assert report["final_decision"]["ALPHA_DATA_BINDING_READY"] is False
    assert "GLOBAL_HARD_GATE_BLOCKED" in report["final_decision"]["decision_reason_codes"]


def test_campaign_a22_failure_blocks_only_that_campaign_but_global_flag_remains_false() -> None:
    report = _valid_report()
    item = report["campaign_readiness"][0]
    item["gate_statuses"]["A22"] = {
        "status": "FAIL",
        "failure_codes": ["A22_INDEPENDENT_SAMPLES_BELOW_MINIMUM"],
    }
    item["independent_samples"] = item["minimum_independent_samples"] - 1
    item["training_validation_ready"] = False
    item["binding_ready"] = False
    item["block_reason_codes"] = ["A22_INDEPENDENT_SAMPLES_BELOW_MINIMUM"]
    report["gates"]["A22"]["status"] = "FAIL"
    report["gates"]["A22"]["failure_codes"] = ["A22_INDEPENDENT_SAMPLES_BELOW_MINIMUM"]
    _refresh(report)
    validate_alpha_data_binding_report(report)
    assert len(report["final_decision"]["campaigns_ready"]) == 5
    assert report["final_decision"]["ALPHA_DATA_BINDING_READY"] is False


def test_filter_gate_pass_allows_legitimate_a19_a20_exclusions() -> None:
    report = _valid_report()
    # Legitimate filtering is represented by exclusion counts, not a gate failure.
    assert any(item["excluded_instrument_times"]["A19"] > 0 for item in report["campaign_readiness"])
    assert report["gates"]["A19"]["status"] == "PASS"
    assert report["gates"]["A20"]["status"] == "PASS"
    validate_alpha_data_binding_report(report)


def test_filter_evaluator_blocked_forces_binding_false() -> None:
    report = _valid_report()
    report["gates"]["A20"]["status"] = "BLOCKED"
    report["gates"]["A20"]["failure_codes"] = ["A20_LIQUIDITY_WINDOW_INCOMPLETE"]
    report["gates"]["A20"]["evidence"] = []
    for item in report["campaign_readiness"]:
        item["binding_ready"] = False
        item["block_reason_codes"] = ["A20_LIQUIDITY_WINDOW_INCOMPLETE"]
    _refresh(report)
    validate_alpha_data_binding_report(report)
    assert "INSTRUMENT_FILTER_EVALUATION_BLOCKED" in report["final_decision"]["decision_reason_codes"]


def test_oos_violation_is_irreversible_for_that_report() -> None:
    report = _valid_report()
    item = report["campaign_readiness"][0]
    item["gate_statuses"]["A26"] = {"status": "FAIL", "failure_codes": ["A26_OOS_SEAL_BROKEN"]}
    item["locked_oos_status"] = "VIOLATED"
    item["binding_ready"] = False
    item["block_reason_codes"] = ["A26_OOS_SEAL_BROKEN"]
    report["gates"]["A26"]["status"] = "FAIL"
    report["gates"]["A26"]["failure_codes"] = ["A26_OOS_SEAL_BROKEN"]
    _refresh(report)
    validate_alpha_data_binding_report(report)
    assert "LOCKED_OOS_SEAL_BROKEN" in report["final_decision"]["decision_reason_codes"]
    assert report["final_decision"]["locked_oos_may_be_opened"] is False


def test_final_decision_cannot_be_asserted_by_hand() -> None:
    report = _valid_report()
    report["final_decision"]["ALPHA_DATA_BINDING_READY"] = False
    with pytest.raises(AlphaDataBindingError, match="final_decision"):
        validate_alpha_data_binding_report(report)


def test_decision_fingerprint_is_independent_of_generated_at_and_human_notes() -> None:
    a = _valid_report()
    b = deepcopy(a)
    b["generated_at"] = "2030-01-01T00:00:00+00:00"
    b["gates"]["A01"]["notes"] = "human-readable note that is deliberately not fingerprinted"
    _refresh(b)
    assert a["final_decision"]["decision_fingerprint"] == b["final_decision"]["decision_fingerprint"]
    validate_alpha_data_binding_report(b)


def test_frozen_failure_code_catalog_covers_all_28_gates() -> None:
    assert tuple(sorted(FAILURE_CODES)) == GATE_IDS
    assert all(codes == tuple(sorted(set(codes))) for codes in FAILURE_CODES.values())
    assert set(GLOBAL_HARD_GATES).issubset(GATE_IDS)
