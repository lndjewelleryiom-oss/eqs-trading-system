from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_ID = "EQS-R1.3-HISTORICAL-CERTIFICATION-MANIFEST-v1"
SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas" / "r1_3_historical_certification_manifest_v1.schema.json"
SEMANTIC_RULE_IDS = tuple(f"R13-M{i:03d}" for i in range(1, 25))


@dataclass(frozen=True, slots=True)
class SemanticValidationResult:
    passed_rules: tuple[str, ...]


class R13ManifestSchemaError(ValueError):
    def __init__(self, message: str, *, path: str = "$"):
        self.path = path
        super().__init__(message)


class R13SemanticRuleError(ValueError):
    def __init__(self, rule_id: str, message: str):
        if rule_id not in SEMANTIC_RULE_IDS:
            raise ValueError(f"unknown R1.3 semantic rule: {rule_id}")
        self.rule_id = rule_id
        super().__init__(f"{rule_id}: {message}")


def canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def payload_sha256(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()


def manifest_content_hash(manifest: Mapping[str, object]) -> str:
    payload = dict(manifest)
    payload.pop("manifest_hash", None)
    return payload_sha256(payload)


def _load_schema() -> Mapping[str, object]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_manifest_schema(manifest: Mapping[str, object]) -> None:
    validator = Draft202012Validator(_load_schema(), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(manifest), key=lambda exc: tuple(str(x) for x in exc.path))
    if errors:
        err = errors[0]
        location = ".".join(str(x) for x in err.absolute_path) or "$"
        raise R13ManifestSchemaError(f"{location}: {err.message}", path=location)


def _dt(value: object) -> datetime:
    if not isinstance(value, str):
        raise TypeError("timestamp must be a string")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = parent[key]
    if not isinstance(value, Mapping):
        raise TypeError(f"{key} must be an object")
    return value


def _records(parent: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    value = parent[key]
    if not isinstance(value, list) or any(not isinstance(item, Mapping) for item in value):
        raise TypeError(f"{key} must be an array of objects")
    return list(value)


def _fail(rule: str, message: str) -> None:
    raise R13SemanticRuleError(rule, message)


def _resolve_artifact(path_or_uri: object, artifact_root: Path | None) -> Path:
    if not isinstance(path_or_uri, str) or not path_or_uri:
        _fail("R13-M021", "artifact path is empty")
    if "://" in path_or_uri:
        _fail("R13-M021", f"remote artifact cannot be byte-verified: {path_or_uri}")
    candidate = Path(path_or_uri)
    if not candidate.is_absolute():
        if artifact_root is None:
            _fail("R13-M021", "artifact_root is required for relative artifact paths")
        candidate = artifact_root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        _fail("R13-M021", f"artifact unavailable: {candidate}: {exc}")
    if not resolved.is_file():
        _fail("R13-M021", f"artifact is not a file: {resolved}")
    return resolved


def validate_r13_semantics(
    manifest: Mapping[str, object],
    *,
    artifact_root: str | Path | None = None,
    prior_manifests: Sequence[Mapping[str, object]] = (),
) -> SemanticValidationResult:
    passed: list[str] = []
    dataset = _mapping(manifest, "dataset")
    feature = _mapping(manifest, "feature_binding")
    universe = _mapping(manifest, "universe")
    lifecycle = _mapping(manifest, "lifecycle")
    coverage = _mapping(manifest, "coverage")
    eligibility = _mapping(manifest, "eligibility")
    validator = _mapping(manifest, "validator")
    catalogue = _mapping(manifest, "rule_catalogue")
    provenance = _mapping(manifest, "provenance")
    pit = _mapping(manifest, "pit")

    if not _dt(dataset["coverage_start"]) < _dt(dataset["coverage_end"]):
        _fail("R13-M001", "coverage_start must be before coverage_end")
    passed.append("R13-M001")

    if not _dt(dataset["coverage_end"]) <= _dt(dataset["decision_time"]):
        _fail("R13-M002", "coverage_end must not exceed decision_time")
    passed.append("R13-M002")

    if not _dt(validator["run_started_at"]) <= _dt(validator["run_completed_at"]) <= _dt(manifest["created_at"]):
        _fail("R13-M003", "validator timestamps are not ordered")
    passed.append("R13-M003")

    if feature["dataset_manifest_fingerprint"] != dataset["dataset_manifest_fingerprint"]:
        _fail("R13-M004", "feature dataset fingerprint mismatch")
    passed.append("R13-M004")

    if feature["universe_fingerprint"] != universe["universe_fingerprint"]:
        _fail("R13-M005", "feature universe fingerprint mismatch")
    passed.append("R13-M005")

    if provenance["universe_snapshot_sha256"] != universe["snapshot_sha256"]:
        _fail("R13-M006", "universe snapshot provenance mismatch")
    passed.append("R13-M006")

    if provenance["lifecycle_event_set_sha256"] != lifecycle["event_set_sha256"]:
        _fail("R13-M007", "lifecycle event-set provenance mismatch")
    passed.append("R13-M007")

    if provenance["coverage_record_set_sha256"] != coverage["coverage_record_set_sha256"]:
        _fail("R13-M008", "coverage record-set provenance mismatch")
    passed.append("R13-M008")

    if provenance["eligibility_record_set_sha256"] != eligibility["eligibility_record_set_sha256"]:
        _fail("R13-M009", "eligibility record-set provenance mismatch")
    passed.append("R13-M009")

    if provenance["validator_version"] != validator["validator_version"]:
        _fail("R13-M010", "validator version provenance mismatch")
    passed.append("R13-M010")

    if provenance["validator_build_sha256"] != validator["validator_build_sha256"]:
        _fail("R13-M011", "validator build provenance mismatch")
    passed.append("R13-M011")

    if provenance["rule_catalog_version"] != catalogue["catalogue_version"]:
        _fail("R13-M012", "rule-catalogue version provenance mismatch")
    passed.append("R13-M012")

    if provenance["rule_catalog_sha256"] != catalogue["catalogue_sha256"]:
        _fail("R13-M013", "rule-catalogue hash provenance mismatch")
    passed.append("R13-M013")

    if provenance["pit_certification_sha256"] != pit["certification_sha256"]:
        _fail("R13-M014", "PIT certification provenance mismatch")
    passed.append("R13-M014")

    eligibility_records = _records(eligibility, "records")
    if eligibility["record_count"] != len(eligibility_records):
        _fail("R13-M015", "eligibility record_count differs from records length")
    passed.append("R13-M015")

    coverage_records = _records(coverage, "records")
    for record in coverage_records:
        if int(record["observed_intervals"]) + int(record["missing_intervals"]) > int(record["expected_intervals"]):
            _fail("R13-M016", f"coverage arithmetic invalid for {record['coverage_id']}")
    passed.append("R13-M016")

    for record in coverage_records:
        if record["availability_status"] == "COMPLETE" and (
            int(record["missing_intervals"]) != 0 or int(record["partial_intervals"]) != 0
        ):
            _fail("R13-M017", f"COMPLETE coverage has missing/partial intervals: {record['coverage_id']}")
    passed.append("R13-M017")

    for record in eligibility_records:
        effective_to = record["effective_to"]
        if effective_to is not None and not _dt(record["effective_from"]) < _dt(effective_to):
            _fail("R13-M018", f"eligibility interval invalid: {record['eligibility_id']}")
    passed.append("R13-M018")

    for record in eligibility_records:
        if record["source_universe_fingerprint"] != universe["universe_fingerprint"]:
            _fail("R13-M019", f"eligibility universe mismatch: {record['eligibility_id']}")
    passed.append("R13-M019")

    artifacts = _records(manifest, "artifact_inventory")
    inventory_hashes = {str(item["sha256"]) for item in artifacts}
    provenance_hash_keys = (
        "h02_batch_manifest_sha256",
        "universe_snapshot_sha256",
        "lifecycle_event_set_sha256",
        "coverage_record_set_sha256",
        "eligibility_record_set_sha256",
        "validator_build_sha256",
        "rule_catalog_sha256",
        "pit_certification_sha256",
        "source_receipt_index_sha256",
        "parent_archive_sha256",
    )
    missing = [key for key in provenance_hash_keys if provenance[key] not in inventory_hashes]
    if missing:
        _fail("R13-M020", "artifact inventory missing provenance hashes: " + ",".join(missing))
    passed.append("R13-M020")

    root = Path(artifact_root).resolve() if artifact_root is not None else None
    for artifact in artifacts:
        path = _resolve_artifact(artifact["path_or_uri"], root)
        data = path.read_bytes()
        actual = sha256(data).hexdigest()
        if actual != artifact["sha256"]:
            _fail("R13-M021", f"artifact hash mismatch: {artifact['artifact_id']}")
        if len(data) != int(artifact["byte_size"]):
            _fail("R13-M021", f"artifact byte_size mismatch: {artifact['artifact_id']}")
    passed.append("R13-M021")

    if manifest["manifest_hash"] != manifest_content_hash(manifest):
        _fail("R13-M022", "manifest_hash does not match canonical content")
    passed.append("R13-M022")

    dataset_id = dataset["dataset_id"]
    generation = int(manifest["manifest_generation"])
    same_lineage = [
        item for item in prior_manifests
        if isinstance(item, Mapping)
        and isinstance(item.get("dataset"), Mapping)
        and item["dataset"].get("dataset_id") == dataset_id
    ]
    if same_lineage:
        prior_generation = max(int(item["manifest_generation"]) for item in same_lineage)
        if generation <= prior_generation:
            _fail("R13-M023", f"manifest_generation {generation} must exceed {prior_generation}")
    passed.append("R13-M023")

    manifest_id = manifest["manifest_id"]
    for prior in prior_manifests:
        if prior.get("manifest_id") == manifest_id and prior.get("manifest_hash") != manifest["manifest_hash"]:
            _fail("R13-M024", "manifest_id is already bound to a different manifest_hash")
    passed.append("R13-M024")

    return SemanticValidationResult(tuple(passed))


def validate_canonical_r13_manifest(
    manifest: Mapping[str, object],
    *,
    artifact_root: str | Path | None = None,
    prior_manifests: Sequence[Mapping[str, object]] = (),
) -> SemanticValidationResult:
    if not manifest:
        raise R13ManifestSchemaError("manifest is empty")
    validate_manifest_schema(manifest)
    return validate_r13_semantics(
        manifest, artifact_root=artifact_root, prior_manifests=prior_manifests
    )
