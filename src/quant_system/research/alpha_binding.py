from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from .campaigns import institutional_crypto_perp_campaigns

SCHEMA_VERSION = "eqs-alpha-data-binding-gates-v1"
DATASET_FORMAT = "crypto-perps-research-v1"
FEATURE_ENGINE_VERSION = "crypto-perps-feature-engine-v1"
EVIDENCE_CLASSIFICATION = "REAL_MARKET"

GATE_IDS = tuple(f"A{i:02d}" for i in range(1, 29))
GLOBAL_HARD_GATES = tuple(
    [f"A{i:02d}" for i in range(1, 19)] + ["A24", "A25", "A27", "A28"]
)
FILTER_GATES = ("A19", "A20")
CAMPAIGN_GATES = ("A21", "A22", "A23", "A26")

VALID_STATUSES = ("PASS", "FAIL", "BLOCKED")
VALID_EVIDENCE_CLASSES = (
    "REAL_MARKET",
    "DERIVED_REAL_MARKET",
    "CONTROLLED_FAULT_INJECTION",
    "TEST_FIXTURE",
)
VALID_EVIDENCE_TYPES = (
    "RAW_RECEIPT_INDEX", "RAW_OBJECT", "PARTITION", "PARTITION_INDEX", "MANIFEST",
    "UNIVERSE_HISTORY", "UNIVERSE_INDEX", "REPLAY_REPORT", "DETERMINISM_REPORT",
    "PIT_AUDIT", "COVERAGE_REPORT", "SAMPLE_REPORT", "FEATURE_COVERAGE_REPORT",
    "FEATURE_RUN_MANIFEST", "TAMPER_TEST", "TEST_LOG", "HASH_REPORT",
    "PROTECTED_BOUNDARY_REPORT", "OTHER",
)

_GATE_CONTRACT: dict[str, tuple[str, str]] = {
    **{f"A{i:02d}": ("GLOBAL", "HARD_BLOCK") for i in range(1, 19)},
    "A19": ("INSTRUMENT_FILTER", "FILTER"),
    "A20": ("INSTRUMENT_FILTER", "FILTER"),
    "A21": ("CAMPAIGN", "CAMPAIGN_BLOCK"),
    "A22": ("CAMPAIGN", "CAMPAIGN_BLOCK"),
    "A23": ("CAMPAIGN", "CAMPAIGN_BLOCK"),
    "A24": ("GLOBAL", "HARD_BLOCK"),
    "A25": ("GLOBAL", "HARD_BLOCK"),
    "A26": ("CAMPAIGN", "CAMPAIGN_INVALIDATION"),
    "A27": ("GLOBAL", "HARD_BLOCK"),
    "A28": ("GLOBAL", "HARD_BLOCK"),
}

FAILURE_CODES: dict[str, tuple[str, ...]] = {
    "A01": ("A01_BUILD_COMMAND_FINGERPRINT_MISMATCH", "A01_BUILD_COMMAND_FINGERPRINT_MISSING", "A01_CODE_VERSION_MISSING", "A01_DATASET_BUILDER_VERSION_MISSING", "A01_PARENT_ARCHIVE_MISSING", "A01_PARENT_SHA_MISMATCH", "A01_PARENT_SHA_MISSING"),
    "A02": ("A02_NON_REAL_MARKET_SOURCE", "A02_SYNTHETIC_INPUT_IN_EMPIRICAL_SET", "A02_TEST_FIXTURE_IN_EMPIRICAL_SET", "A02_UNCLASSIFIED_EMPIRICAL_INPUT"),
    "A03": ("A03_EVENT_RAW_SHA_MISSING", "A03_RAW_OBJECT_MISSING", "A03_RAW_RECEIPT_DUPLICATE_CONFLICT", "A03_RAW_RECEIPT_MISSING", "A03_RAW_SHA_MISMATCH"),
    "A04": ("A04_AVAILABILITY_AFTER_RECEIPT_INVALID", "A04_BACKWARD_AVAILABILITY_ADJUSTMENT", "A04_EVENT_AFTER_PUBLICATION_INVALID", "A04_PUBLICATION_AFTER_AVAILABILITY_INVALID", "A04_TIMESTAMP_EVIDENCE_MISSING"),
    "A05": ("A05_CONTENT_ADDRESS_MISMATCH", "A05_MUTABLE_PARTITION_PATH", "A05_PARTITION_HASH_MISMATCH", "A05_TAMPER_NOT_DETECTED", "A05_TAMPER_TEST_MISSING"),
    "A06": ("A06_AVAILABILITY_RANGE_MISMATCH", "A06_DESCRIPTOR_MISSING", "A06_PARTITION_KEY_MISMATCH", "A06_PARTITION_SHA_DESCRIPTOR_MISMATCH", "A06_ROW_COUNT_MISMATCH", "A06_TIME_RANGE_MISMATCH"),
    "A07": ("A07_CONFLICTING_DUPLICATE_EVENT", "A07_DUPLICATE_EVENT_IDENTITY", "A07_MIXED_DATASET_ID", "A07_MIXED_EVENT_KIND", "A07_MIXED_INSTRUMENT", "A07_MIXED_VENUE", "A07_NONCANONICAL_ROW_ORDER", "A07_SCHEMA_VERSION_MIX"),
    "A08": ("A08_DATASET_FORMAT_MISMATCH", "A08_MANIFEST_FINGERPRINT_MISMATCH", "A08_MANIFEST_MISSING", "A08_MANIFEST_PARTITION_REFERENCE_INVALID"),
    "A09": ("A09_EVENT_COUNT_MISMATCH", "A09_EVENT_IDENTITY_HASH_MISMATCH", "A09_MANIFEST_EVENT_MISSING", "A09_UNMANIFESTED_EVENT_PRESENT"),
    "A10": ("A10_EVENT_IDENTITY_NONDETERMINISM", "A10_MANIFEST_NONDETERMINISM", "A10_PARTITION_NONDETERMINISM", "A10_REPLAY_NONDETERMINISM", "A10_SECOND_BUILD_MISSING", "A10_UNIVERSE_NONDETERMINISM"),
    "A11": ("A11_REPLAY_CROSSED_DECISION_BOUNDARY", "A11_REPLAY_EVENT_SET_MISMATCH", "A11_REPLAY_FINGERPRINT_MISMATCH", "A11_REPLAY_ORDER_MISMATCH"),
    "A12": ("A12_FUTURE_AVAILABLE_EVENT_INCLUDED", "A12_FUTURE_INSTRUMENT_STATE_INCLUDED", "A12_FUTURE_REVISION_INCLUDED", "A12_PIT_AUDIT_INCOMPLETE"),
    "A13": ("A13_DEFINITION_LINEAGE_MISSING", "A13_UNIVERSE_FINGERPRINT_MISMATCH", "A13_UNIVERSE_HISTORY_MISSING", "A13_UNIVERSE_HISTORY_MUTATED"),
    "A14": ("A14_DEFINITION_NOT_YET_AVAILABLE", "A14_EVENT_BEFORE_EFFECTIVE_LISTING", "A14_EVENT_WHILE_INACTIVE", "A14_NO_ASOF_DEFINITION"),
    "A15": ("A15_HISTORICAL_INSTRUMENT_DROPPED", "A15_PRESENT_UNIVERSE_PROJECTED_BACKWARD", "A15_SURVIVORSHIP_FILTER_DETECTED"),
    "A16": ("A16_DELISTING_TRANSITION_MISSING", "A16_LISTING_TRANSITION_MISSING", "A16_REVISION_SOURCE_COVERAGE_INSUFFICIENT", "A16_SOURCE_REVISION_MISSING", "A16_SPEC_REVISION_MISSING"),
    "A17": ("A17_NON_LINEAR_CONTRACT", "A17_NON_PERPETUAL_INSTRUMENT", "A17_UNREGISTERED_INSTRUMENT_TYPE", "A17_UNSUPPORTED_VENUE"),
    "A18": ("A18_COVERAGE_REPORT_MISSING", "A18_OOS_WINDOW_INCOMPLETE", "A18_REQUIRED_SERIES_GAP", "A18_TRAINING_WINDOW_INCOMPLETE", "A18_VALIDATION_WINDOW_INCOMPLETE"),
    "A19": ("A19_HISTORY_AGE_UNEVALUABLE", "A19_HISTORY_LT_90D", "A19_INELIGIBLE_INSTRUMENT_ADMITTED", "A19_LISTING_TIME_UNKNOWN"),
    "A20": ("A20_ILLIQUID_INSTRUMENT_ADMITTED", "A20_LIQUIDITY_BELOW_THRESHOLD", "A20_LIQUIDITY_NON_PIT_INPUT", "A20_LIQUIDITY_VALUE_MISSING", "A20_LIQUIDITY_WINDOW_INCOMPLETE"),
    "A21": ("A21_EMBARGO_LT_24H", "A21_FOLD_CONSTRUCTION_BLOCKED", "A21_FOLD_OVERLAP_INVALID", "A21_INSUFFICIENT_FOLDS", "A21_PURGE_LT_48H", "A21_TEST_WINDOW_SHORT", "A21_TRAIN_WINDOW_SHORT"),
    "A22": ("A22_CROSS_SECTION_INSUFFICIENT_BREADTH", "A22_DEPENDENCE_HORIZON_MISSING", "A22_DEPENDENCE_HORIZON_USES_OOS", "A22_INDEPENDENT_SAMPLES_BELOW_MINIMUM", "A22_NO_ELIGIBLE_SAMPLE"),
    "A23": ("A23_FEATURE_COVERAGE_INSUFFICIENT", "A23_FEATURE_GENERATION_FAILURE", "A23_FEATURE_SOURCE_UNAVAILABLE", "A23_REQUIRED_FEATURE_MISSING", "A23_SILENT_FEATURE_IMPUTATION"),
    "A24": ("A24_DATASET_FINGERPRINT_MISMATCH", "A24_FEATURE_ENGINE_VERSION_MISMATCH", "A24_FEATURE_RUN_LINEAGE_MISSING", "A24_FEATURE_RUN_MANIFEST_MISSING", "A24_UNIVERSE_FINGERPRINT_MISMATCH"),
    "A25": ("A25_FEATURE_FUTURE_SOURCE", "A25_LEAKAGE_GUARD_DID_NOT_FAIL_CLOSED", "A25_LEAKAGE_GUARD_MISSING", "A25_SOURCE_MAX_AVAILABLE_AFTER_DECISION"),
    "A26": ("A26_OOS_ACCESS_LOG_MISSING", "A26_OOS_OUTCOME_VIEWED", "A26_OOS_SEAL_BROKEN", "A26_OOS_USED_FOR_PARAMETER_SELECTION", "A26_OOS_USED_FOR_PRUNING", "A26_OOS_USED_FOR_RANKING"),
    "A27": ("A27_DATASET_MUTATED_AFTER_ACCEPTANCE", "A27_FINAL_ARTIFACT_HASH_MISMATCH", "A27_FINAL_HASH_INDEX_MISSING", "A27_MANIFEST_MUTATED_AFTER_ACCEPTANCE", "A27_UNIVERSE_MUTATED_AFTER_ACCEPTANCE"),
    "A28": ("A28_BROKER_SUBMISSION_ENABLED", "A28_COMPILE_FAILURE", "A28_CREDENTIAL_ACCESS_DETECTED", "A28_F7_CHANGED", "A28_LIVE_CAPITAL_ACCESS_DETECTED", "A28_PROTECTED_BOUNDARY_CHANGED", "A28_R1_2_CHANGED", "A28_REGRESSION_FAILURE", "A28_TEST_DATA_GUARD_FAILURE", "A28_TRACKER_CHANGED"),
}

SUMMARY_READY_CODES = (
    "ALL_GLOBAL_HARD_GATES_PASS",
    "ALL_INSTRUMENT_FILTERS_APPLIED",
    "ALL_CAMPAIGNS_BINDING_READY",
    "LOCKED_OOS_SEAL_INTACT",
    "ALPHA_V1_DATA_BINDING_ACCEPTED",
)
SUMMARY_BLOCK_CODES = (
    "ALPHA_V1_DATA_BINDING_REJECTED",
    "FEATURE_COVERAGE_INCOMPLETE",
    "GLOBAL_HARD_GATE_BLOCKED",
    "GLOBAL_HARD_GATE_FAILURE",
    "INSUFFICIENT_INDEPENDENT_SAMPLES",
    "INSUFFICIENT_WALK_FORWARD_COVERAGE",
    "INSTRUMENT_FILTER_EVALUATION_BLOCKED",
    "LOCKED_OOS_SEAL_BROKEN",
    "LOCKED_OOS_SEAL_NOT_PROVEN",
    "ONE_OR_MORE_CAMPAIGNS_BLOCKED",
)

_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class AlphaDataBindingError(ValueError):
    pass


def canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def sha256_payload(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()


def frozen_campaign_contracts() -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for campaign in institutional_crypto_perp_campaigns():
        minimums = dict(campaign.minimum_requirements)
        liquidity_rule = next(rule for rule in campaign.universe_rules if "rolling 30-day median quote volume" in rule)
        threshold = int(liquidity_rule.split(">=", 1)[1].split("USD", 1)[0].strip())
        result[campaign.campaign_id] = {
            "campaign_fingerprint": campaign.fingerprint,
            "minimum_independent_samples": int(minimums["independent_samples"]),
            "minimum_candidate_trades": int(minimums["trades"]),
            "minimum_walk_forward_folds": 6,
            "liquidity_threshold_usd_per_day": threshold,
        }
    return dict(sorted(result.items()))


def compute_campaign_set_fingerprint(campaigns: Sequence[Mapping[str, object]]) -> str:
    canonical = [
        {
            "campaign_id": str(item["campaign_id"]),
            "campaign_fingerprint": str(item["campaign_fingerprint"]),
        }
        for item in campaigns
    ]
    canonical.sort(key=lambda item: item["campaign_id"])
    return sha256_payload(canonical)


def compute_binding_id(report: Mapping[str, object]) -> str:
    parent = _mapping(report, "parent")
    dataset = _mapping(report, "dataset")
    feature = _mapping(report, "feature_engine")
    campaign_set = _mapping(report, "alpha_campaign_set")
    campaigns = _sequence(campaign_set, "campaigns")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "parent_archive_sha256": parent.get("archive_sha256"),
        "dataset_manifest_fingerprint": dataset.get("manifest_fingerprint"),
        "universe_fingerprint": dataset.get("universe_fingerprint"),
        "feature_engine_version": feature.get("version"),
        "campaigns": sorted(
            ({"campaign_id": item.get("campaign_id"), "campaign_fingerprint": item.get("campaign_fingerprint")} for item in campaigns if isinstance(item, Mapping)),
            key=lambda item: str(item["campaign_id"]),
        ),
    }
    return sha256_payload(payload)


def _mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise AlphaDataBindingError(f"{key} must be an object")
    return value


def _sequence(parent: Mapping[str, object], key: str) -> list[object]:
    value = parent.get(key)
    if not isinstance(value, list):
        raise AlphaDataBindingError(f"{key} must be an array")
    return value


def _require_sha(value: object, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise AlphaDataBindingError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _require_sorted_unique_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise AlphaDataBindingError(f"{name} must be an array of strings")
    if value != sorted(set(value)):
        raise AlphaDataBindingError(f"{name} must be sorted and unique")
    return tuple(value)


def _validate_evidence(evidence: object, gate_id: str, status: str) -> None:
    if not isinstance(evidence, list):
        raise AlphaDataBindingError(f"{gate_id}.evidence must be an array")
    if status in {"PASS", "FAIL"} and not evidence:
        raise AlphaDataBindingError(f"{gate_id} {status} requires objective evidence")
    for index, item in enumerate(evidence):
        if not isinstance(item, Mapping):
            raise AlphaDataBindingError(f"{gate_id}.evidence[{index}] must be an object")
        if item.get("evidence_type") not in VALID_EVIDENCE_TYPES:
            raise AlphaDataBindingError(f"{gate_id}.evidence[{index}] has invalid evidence_type")
        if item.get("classification") not in VALID_EVIDENCE_CLASSES:
            raise AlphaDataBindingError(f"{gate_id}.evidence[{index}] has invalid classification")
        if not isinstance(item.get("uri_or_path"), str) or not str(item["uri_or_path"]).strip():
            raise AlphaDataBindingError(f"{gate_id}.evidence[{index}].uri_or_path is required")
        _require_sha(item.get("sha256"), f"{gate_id}.evidence[{index}].sha256")


def _validate_gate(gate_id: str, gate: object) -> None:
    if not isinstance(gate, Mapping):
        raise AlphaDataBindingError(f"{gate_id} must be an object")
    if gate.get("gate_id") != gate_id:
        raise AlphaDataBindingError(f"{gate_id}.gate_id mismatch")
    expected_scope, expected_severity = _GATE_CONTRACT[gate_id]
    if gate.get("scope") != expected_scope or gate.get("severity") != expected_severity:
        raise AlphaDataBindingError(f"{gate_id} scope/severity does not match the frozen evaluator contract")
    status = gate.get("status")
    if status not in VALID_STATUSES:
        raise AlphaDataBindingError(f"{gate_id}.status is invalid")
    if not isinstance(gate.get("name"), str) or not str(gate["name"]).strip():
        raise AlphaDataBindingError(f"{gate_id}.name is required")
    if not isinstance(gate.get("pass_condition"), str) or not str(gate["pass_condition"]).strip():
        raise AlphaDataBindingError(f"{gate_id}.pass_condition is required")
    if not isinstance(gate.get("observed"), Mapping):
        raise AlphaDataBindingError(f"{gate_id}.observed must be an object")
    codes = _require_sorted_unique_strings(gate.get("failure_codes"), f"{gate_id}.failure_codes")
    unknown = set(codes) - set(FAILURE_CODES[gate_id])
    if unknown:
        raise AlphaDataBindingError(f"{gate_id} contains unknown failure code(s): {sorted(unknown)}")
    if status == "PASS" and codes:
        raise AlphaDataBindingError(f"{gate_id} PASS cannot contain failure codes")
    if status in {"FAIL", "BLOCKED"} and not codes:
        raise AlphaDataBindingError(f"{gate_id} {status} requires at least one failure code")
    _validate_evidence(gate.get("evidence"), gate_id, str(status))


def _validate_campaign_gate_status(campaign_id: str, gate_id: str, value: object) -> None:
    if gate_id not in CAMPAIGN_GATES:
        raise AlphaDataBindingError(f"{gate_id} is not a campaign gate")
    if not isinstance(value, Mapping):
        raise AlphaDataBindingError(f"{campaign_id}.{gate_id} must be an object")
    status = value.get("status")
    if status not in VALID_STATUSES:
        raise AlphaDataBindingError(f"{campaign_id}.{gate_id}.status invalid")
    codes = _require_sorted_unique_strings(value.get("failure_codes"), f"{campaign_id}.{gate_id}.failure_codes")
    unknown = set(codes) - set(FAILURE_CODES[gate_id])
    if unknown:
        raise AlphaDataBindingError(f"{campaign_id}.{gate_id} contains unknown failure code(s): {sorted(unknown)}")
    if status == "PASS" and codes:
        raise AlphaDataBindingError(f"{campaign_id}.{gate_id} PASS cannot contain failure codes")
    if status in {"FAIL", "BLOCKED"} and not codes:
        raise AlphaDataBindingError(f"{campaign_id}.{gate_id} {status} requires a failure code")


def _validate_campaign_readiness(items: object) -> list[Mapping[str, object]]:
    if not isinstance(items, list):
        raise AlphaDataBindingError("campaign_readiness must be an array")
    contracts = frozen_campaign_contracts()
    if len(items) != len(contracts):
        raise AlphaDataBindingError("campaign_readiness must contain exactly the six frozen Alpha-v1 campaigns")
    by_id: dict[str, Mapping[str, object]] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise AlphaDataBindingError("campaign_readiness entries must be objects")
        campaign_id = item.get("campaign_id")
        if campaign_id not in contracts or campaign_id in by_id:
            raise AlphaDataBindingError("campaign_readiness contains an unknown or duplicate campaign")
        contract = contracts[str(campaign_id)]
        _require_sha(item.get("campaign_fingerprint"), f"{campaign_id}.campaign_fingerprint")
        if item.get("campaign_fingerprint") != contract["campaign_fingerprint"]:
            raise AlphaDataBindingError(f"{campaign_id} fingerprint differs from frozen Alpha-v1 preregistration")
        for field in ("eligible_instrument_count", "eligible_observations", "independent_samples", "walk_forward_fold_count"):
            value = item.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise AlphaDataBindingError(f"{campaign_id}.{field} must be a non-negative integer")
        if item.get("minimum_independent_samples") != contract["minimum_independent_samples"]:
            raise AlphaDataBindingError(f"{campaign_id}.minimum_independent_samples differs from frozen contract")
        if item.get("minimum_candidate_trades") != contract["minimum_candidate_trades"]:
            raise AlphaDataBindingError(f"{campaign_id}.minimum_candidate_trades differs from frozen contract")
        if item.get("minimum_walk_forward_folds") != 6:
            raise AlphaDataBindingError(f"{campaign_id}.minimum_walk_forward_folds must be 6")
        if item.get("candidate_trade_count_status") != "DEFERRED_UNTIL_CANDIDATE_EXECUTION":
            raise AlphaDataBindingError(f"{campaign_id} candidate trade count must remain deferred at dataset-binding time")
        for field in ("feature_coverage_complete", "training_validation_ready", "binding_ready"):
            if not isinstance(item.get(field), bool):
                raise AlphaDataBindingError(f"{campaign_id}.{field} must be boolean")
        if item.get("locked_oos_status") not in {"SEALED", "VIOLATED"}:
            raise AlphaDataBindingError(f"{campaign_id}.locked_oos_status invalid")
        _require_sha(item.get("eligibility_mask_fingerprint"), f"{campaign_id}.eligibility_mask_fingerprint")
        excluded = item.get("excluded_instrument_times")
        if not isinstance(excluded, Mapping) or set(excluded) != {"A19", "A20", "A19_A20"}:
            raise AlphaDataBindingError(f"{campaign_id}.excluded_instrument_times must contain A19/A20/A19_A20")
        if any(not isinstance(v, int) or isinstance(v, bool) or v < 0 for v in excluded.values()):
            raise AlphaDataBindingError(f"{campaign_id}.excluded_instrument_times values must be non-negative integers")
        gate_statuses = item.get("gate_statuses")
        if not isinstance(gate_statuses, Mapping) or set(gate_statuses) != set(CAMPAIGN_GATES):
            raise AlphaDataBindingError(f"{campaign_id}.gate_statuses must contain exactly A21,A22,A23,A26")
        for gate_id in CAMPAIGN_GATES:
            _validate_campaign_gate_status(str(campaign_id), gate_id, gate_statuses[gate_id])
        _require_sorted_unique_strings(item.get("block_reason_codes"), f"{campaign_id}.block_reason_codes")
        by_id[str(campaign_id)] = item
    if list(by_id) != sorted(by_id):
        raise AlphaDataBindingError("campaign_readiness must be sorted by campaign_id")
    return [by_id[cid] for cid in sorted(by_id)]


def _expected_campaign_ready(item: Mapping[str, object], gates: Mapping[str, Mapping[str, object]], global_pass: bool) -> bool:
    gate_statuses = _mapping(item, "gate_statuses")
    return bool(
        global_pass
        and gates["A19"].get("status") == "PASS"
        and gates["A20"].get("status") == "PASS"
        and int(item["eligible_instrument_count"]) > 0
        and int(item["eligible_observations"]) > 0
        and int(item["independent_samples"]) >= int(item["minimum_independent_samples"])
        and int(item["walk_forward_fold_count"]) >= int(item["minimum_walk_forward_folds"])
        and bool(item["training_validation_ready"])
        and bool(item["feature_coverage_complete"])
        and item["locked_oos_status"] == "SEALED"
        and all(_mapping(gate_statuses, gate_id).get("status") == "PASS" for gate_id in CAMPAIGN_GATES)
    )


def _summary_reason_codes(gates: Mapping[str, Mapping[str, object]], campaigns: Sequence[Mapping[str, object]], ready: bool) -> tuple[str, ...]:
    if ready:
        return tuple(sorted(SUMMARY_READY_CODES))
    codes: set[str] = {"ALPHA_V1_DATA_BINDING_REJECTED"}
    global_statuses = [str(gates[g].get("status")) for g in GLOBAL_HARD_GATES]
    if "FAIL" in global_statuses:
        codes.add("GLOBAL_HARD_GATE_FAILURE")
    if "BLOCKED" in global_statuses:
        codes.add("GLOBAL_HARD_GATE_BLOCKED")
    if gates["A19"].get("status") != "PASS" or gates["A20"].get("status") != "PASS":
        codes.add("INSTRUMENT_FILTER_EVALUATION_BLOCKED")
    if any(not bool(item.get("binding_ready")) for item in campaigns):
        codes.add("ONE_OR_MORE_CAMPAIGNS_BLOCKED")
    if any(_mapping(_mapping(item, "gate_statuses"), "A21").get("status") != "PASS" for item in campaigns):
        codes.add("INSUFFICIENT_WALK_FORWARD_COVERAGE")
    if any(_mapping(_mapping(item, "gate_statuses"), "A22").get("status") != "PASS" for item in campaigns):
        codes.add("INSUFFICIENT_INDEPENDENT_SAMPLES")
    if any(_mapping(_mapping(item, "gate_statuses"), "A23").get("status") != "PASS" for item in campaigns):
        codes.add("FEATURE_COVERAGE_INCOMPLETE")
    oos_statuses = [_mapping(_mapping(item, "gate_statuses"), "A26").get("status") for item in campaigns]
    if any(item.get("locked_oos_status") == "VIOLATED" or status == "FAIL" for item, status in zip(campaigns, oos_statuses)):
        codes.add("LOCKED_OOS_SEAL_BROKEN")
    elif any(status != "PASS" for status in oos_statuses):
        codes.add("LOCKED_OOS_SEAL_NOT_PROVEN")
    return tuple(sorted(codes))


def _expected_training_validation_ready(item: Mapping[str, object]) -> bool:
    statuses = _mapping(item, "gate_statuses")
    return bool(
        int(item["eligible_instrument_count"]) > 0
        and int(item["eligible_observations"]) > 0
        and _mapping(statuses, "A21").get("status") == "PASS"
        and _mapping(statuses, "A22").get("status") == "PASS"
        and _mapping(statuses, "A23").get("status") == "PASS"
    )


def _expected_campaign_block_codes(
    item: Mapping[str, object], gates: Mapping[str, Mapping[str, object]]
) -> tuple[str, ...]:
    codes: set[str] = set()
    for gate_id in GLOBAL_HARD_GATES + FILTER_GATES:
        gate = gates[gate_id]
        if gate.get("status") != "PASS":
            codes.update(str(code) for code in gate.get("failure_codes", ()))
    statuses = _mapping(item, "gate_statuses")
    for gate_id in CAMPAIGN_GATES:
        gate = _mapping(statuses, gate_id)
        if gate.get("status") != "PASS":
            codes.update(str(code) for code in gate.get("failure_codes", ()))
    return tuple(sorted(codes))


def _validate_campaign_internal_semantics(item: Mapping[str, object]) -> None:
    campaign_id = str(item["campaign_id"])
    statuses = _mapping(item, "gate_statuses")
    if _mapping(statuses, "A21").get("status") == "PASS" and int(item["walk_forward_fold_count"]) < int(item["minimum_walk_forward_folds"]):
        raise AlphaDataBindingError(f"{campaign_id}.A21 PASS requires the minimum walk-forward fold count")
    if _mapping(statuses, "A22").get("status") == "PASS":
        if int(item["independent_samples"]) < int(item["minimum_independent_samples"]):
            raise AlphaDataBindingError(f"{campaign_id}.A22 PASS requires the minimum independent samples")
        if int(item["eligible_observations"]) <= 0:
            raise AlphaDataBindingError(f"{campaign_id}.A22 PASS requires eligible observations")
    if _mapping(statuses, "A23").get("status") == "PASS" and not bool(item["feature_coverage_complete"]):
        raise AlphaDataBindingError(f"{campaign_id}.A23 PASS requires complete feature coverage")
    if _mapping(statuses, "A26").get("status") == "PASS" and item["locked_oos_status"] != "SEALED":
        raise AlphaDataBindingError(f"{campaign_id}.A26 PASS requires SEALED locked OOS")
    expected_tv = _expected_training_validation_ready(item)
    if bool(item["training_validation_ready"]) != expected_tv:
        raise AlphaDataBindingError(f"{campaign_id}.training_validation_ready does not match deterministic campaign-gate roll-up")


def decision_fingerprint_payload(report: Mapping[str, object]) -> dict[str, object]:
    gates = _mapping(report, "gates")
    campaigns = _sequence(report, "campaign_readiness")
    evidence_hashes: set[str] = set()
    gate_payload: dict[str, object] = {}
    for gate_id in GATE_IDS:
        gate = _mapping(gates, gate_id)
        for evidence in _sequence(gate, "evidence"):
            if isinstance(evidence, Mapping) and isinstance(evidence.get("sha256"), str):
                evidence_hashes.add(str(evidence["sha256"]))
        gate_payload[gate_id] = {
            "status": gate.get("status"),
            "failure_codes": gate.get("failure_codes"),
        }
    campaign_payload = []
    for item in campaigns:
        if not isinstance(item, Mapping):
            continue
        campaign_payload.append({
            "campaign_id": item.get("campaign_id"),
            "campaign_fingerprint": item.get("campaign_fingerprint"),
            "eligibility_mask_fingerprint": item.get("eligibility_mask_fingerprint"),
            "eligible_instrument_count": item.get("eligible_instrument_count"),
            "eligible_observations": item.get("eligible_observations"),
            "independent_samples": item.get("independent_samples"),
            "walk_forward_fold_count": item.get("walk_forward_fold_count"),
            "feature_coverage_complete": item.get("feature_coverage_complete"),
            "training_validation_ready": item.get("training_validation_ready"),
            "locked_oos_status": item.get("locked_oos_status"),
            "gate_statuses": item.get("gate_statuses"),
            "binding_ready": item.get("binding_ready"),
        })
    campaign_payload.sort(key=lambda item: str(item["campaign_id"]))
    return {
        "binding_id": report.get("binding_id"),
        "gates": gate_payload,
        "campaign_readiness": campaign_payload,
        "evidence_sha256s": sorted(evidence_hashes),
    }


def compute_decision_fingerprint(report: Mapping[str, object]) -> str:
    return sha256_payload(decision_fingerprint_payload(report))


def expected_final_decision(report: Mapping[str, object]) -> dict[str, object]:
    gates_obj = _mapping(report, "gates")
    gates = {gate_id: _mapping(gates_obj, gate_id) for gate_id in GATE_IDS}
    campaigns = [item for item in _sequence(report, "campaign_readiness") if isinstance(item, Mapping)]
    global_pass = all(gates[gate_id].get("status") == "PASS" for gate_id in GLOBAL_HARD_GATES)
    all_campaigns_ready = all(bool(item.get("binding_ready")) for item in campaigns) and len(campaigns) == 6
    ready = bool(global_pass and gates["A19"].get("status") == "PASS" and gates["A20"].get("status") == "PASS" and all_campaigns_ready)
    return {
        "ALPHA_DATA_BINDING_READY": ready,
        "global_hard_gates_passed": global_pass,
        "campaigns_ready": sorted(str(item["campaign_id"]) for item in campaigns if bool(item.get("binding_ready"))),
        "campaigns_blocked": sorted(str(item["campaign_id"]) for item in campaigns if not bool(item.get("binding_ready"))),
        "locked_oos_may_be_opened": ready,
        "decision_reason_codes": list(_summary_reason_codes(gates, campaigns, ready)),
        "decision_fingerprint": compute_decision_fingerprint(report),
    }


def validate_alpha_data_binding_report(report: Mapping[str, object]) -> None:
    if report.get("schema_version") != SCHEMA_VERSION:
        raise AlphaDataBindingError("schema_version mismatch")
    if report.get("evidence_classification") != EVIDENCE_CLASSIFICATION:
        raise AlphaDataBindingError("binding report evidence_classification must be REAL_MARKET")
    _require_sha(report.get("binding_id"), "binding_id")
    if report.get("binding_id") != compute_binding_id(report):
        raise AlphaDataBindingError("binding_id does not match canonical bound lineage")

    parent = _mapping(report, "parent")
    _require_sha(parent.get("archive_sha256"), "parent.archive_sha256")
    _require_sha(parent.get("build_command_fingerprint"), "parent.build_command_fingerprint")
    for field in ("archive_path", "code_version", "dataset_builder_version"):
        if not isinstance(parent.get(field), str) or not str(parent[field]).strip():
            raise AlphaDataBindingError(f"parent.{field} is required")

    dataset = _mapping(report, "dataset")
    if dataset.get("format_version") != DATASET_FORMAT:
        raise AlphaDataBindingError("dataset.format_version mismatch")
    for field in ("manifest_fingerprint", "universe_fingerprint", "event_identity_hash", "replay_fingerprint"):
        _require_sha(dataset.get(field), f"dataset.{field}")
    for field in ("partition_count", "event_count", "raw_source_count"):
        value = dataset.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise AlphaDataBindingError(f"dataset.{field} must be a positive integer")

    feature = _mapping(report, "feature_engine")
    if feature.get("version") != FEATURE_ENGINE_VERSION:
        raise AlphaDataBindingError("feature_engine.version mismatch")
    if feature.get("dataset_manifest_fingerprint") != dataset.get("manifest_fingerprint"):
        raise AlphaDataBindingError("feature_engine dataset lineage mismatch")
    if feature.get("universe_fingerprint") != dataset.get("universe_fingerprint"):
        raise AlphaDataBindingError("feature_engine universe lineage mismatch")
    runs = _sequence(feature, "run_manifest_fingerprints")
    if not runs:
        raise AlphaDataBindingError("feature_engine.run_manifest_fingerprints cannot be empty")
    if runs != sorted(set(runs)):
        raise AlphaDataBindingError("feature_engine.run_manifest_fingerprints must be sorted and unique")
    for index, value in enumerate(runs):
        _require_sha(value, f"feature_engine.run_manifest_fingerprints[{index}]")

    campaign_set = _mapping(report, "alpha_campaign_set")
    if campaign_set.get("version") != "alpha-campaign-v1":
        raise AlphaDataBindingError("alpha_campaign_set.version mismatch")
    campaigns_raw = _sequence(campaign_set, "campaigns")
    if len(campaigns_raw) != 6 or any(not isinstance(item, Mapping) for item in campaigns_raw):
        raise AlphaDataBindingError("alpha_campaign_set must contain exactly six campaign objects")
    contracts = frozen_campaign_contracts()
    ids = [str(item["campaign_id"]) for item in campaigns_raw]  # type: ignore[index]
    if ids != sorted(contracts):
        raise AlphaDataBindingError("alpha_campaign_set campaigns must be the six frozen campaigns sorted by campaign_id")
    for item in campaigns_raw:
        assert isinstance(item, Mapping)
        cid = str(item["campaign_id"])
        if item.get("campaign_fingerprint") != contracts[cid]["campaign_fingerprint"]:
            raise AlphaDataBindingError(f"{cid} campaign fingerprint mismatch")
    expected_set_fp = compute_campaign_set_fingerprint([item for item in campaigns_raw if isinstance(item, Mapping)])
    if campaign_set.get("campaign_set_fingerprint") != expected_set_fp:
        raise AlphaDataBindingError("campaign_set_fingerprint mismatch")

    gates = _mapping(report, "gates")
    if set(gates) != set(GATE_IDS):
        raise AlphaDataBindingError("gates must contain exactly A01 through A28")
    for gate_id in GATE_IDS:
        _validate_gate(gate_id, gates[gate_id])

    campaign_readiness = _validate_campaign_readiness(report.get("campaign_readiness"))
    global_pass = all(_mapping(gates, gate_id).get("status") == "PASS" for gate_id in GLOBAL_HARD_GATES)
    gate_map = {gate_id: _mapping(gates, gate_id) for gate_id in GATE_IDS}
    for gate_id in CAMPAIGN_GATES:
        campaign_states = [_mapping(_mapping(item, "gate_statuses"), gate_id) for item in campaign_readiness]
        statuses = [str(state.get("status")) for state in campaign_states]
        expected_status = "FAIL" if "FAIL" in statuses else ("BLOCKED" if "BLOCKED" in statuses else "PASS")
        expected_codes = sorted({str(code) for state in campaign_states for code in state.get("failure_codes", ())})
        aggregate = gate_map[gate_id]
        if aggregate.get("status") != expected_status:
            raise AlphaDataBindingError(f"{gate_id} aggregate status does not match campaign gate statuses")
        if list(aggregate.get("failure_codes", ())) != expected_codes:
            raise AlphaDataBindingError(f"{gate_id} aggregate failure_codes do not match campaign gate statuses")
    for item in campaign_readiness:
        _validate_campaign_internal_semantics(item)
        expected_codes = _expected_campaign_block_codes(item, gate_map)
        if tuple(item.get("block_reason_codes", ())) != expected_codes:
            raise AlphaDataBindingError(f"{item['campaign_id']}.block_reason_codes does not match deterministic failure-code roll-up")
        expected = _expected_campaign_ready(item, gate_map, global_pass)
        if bool(item.get("binding_ready")) != expected:
            raise AlphaDataBindingError(f"{item['campaign_id']}.binding_ready does not match deterministic roll-up")

    boundary = _mapping(report, "protected_boundary")
    for field in ("r1_2_unchanged", "f7_unchanged", "tracker_unchanged", "broker_submission_enabled", "credentials_accessed", "live_capital_touched"):
        if not isinstance(boundary.get(field), bool):
            raise AlphaDataBindingError(f"protected_boundary.{field} must be boolean")
    for field in ("protected_files_compared", "protected_files_changed"):
        value = boundary.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise AlphaDataBindingError(f"protected_boundary.{field} must be non-negative integer")
    _require_sha(boundary.get("evidence_sha256"), "protected_boundary.evidence_sha256")
    a28_pass = _mapping(gates, "A28").get("status") == "PASS"
    boundary_safe = bool(
        boundary["r1_2_unchanged"]
        and boundary["f7_unchanged"]
        and boundary["tracker_unchanged"]
        and not boundary["broker_submission_enabled"]
        and not boundary["credentials_accessed"]
        and not boundary["live_capital_touched"]
        and int(boundary["protected_files_changed"]) == 0
    )
    if a28_pass != boundary_safe:
        raise AlphaDataBindingError("A28 status does not match protected_boundary safety facts")

    final = _mapping(report, "final_decision")
    expected = expected_final_decision(report)
    if dict(final) != expected:
        raise AlphaDataBindingError("final_decision does not match deterministic evaluator output")


def load_and_validate_alpha_data_binding_report(path: str | Path) -> dict[str, object]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise AlphaDataBindingError("binding report root must be an object")
    validate_alpha_data_binding_report(payload)
    return payload
