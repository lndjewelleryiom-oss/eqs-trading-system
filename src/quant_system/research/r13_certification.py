from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker

from quant_system.data.crypto_perps.research_datasets import ResearchDatasetManifest

from .alpha_binding import GATE_IDS, validate_alpha_data_binding_report
from .r13_admission import AdmissionRecord, R13AdmissionLedger, validate_r13_manifest
from .r13_manifest import canonical_json, manifest_content_hash

INPUT_SCHEMA_ID = "EQS-R1.3-GENUINE-CERTIFICATION-INPUTS-v1"
INPUT_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3]
    / "schemas"
    / "r1_3_genuine_certification_inputs_v1.schema.json"
)
RR_IDS = tuple(f"RR-{i:03d}" for i in range(1, 31))

ERROR_CODES = (
    "R13_CERT_INPUT_SCHEMA_INVALID",
    "R13_CERT_REQUIRED_ARTIFACT_MISSING",
    "R13_CERT_ARTIFACT_HASH_MISMATCH",
    "R13_CERT_ARTIFACT_CLASSIFICATION_INVALID",
    "R13_CERT_ARTIFACT_TYPE_INVALID",
    "R13_CERT_JSON_INVALID",
    "R13_CERT_H02_INVALID",
    "R13_CERT_PIT_INVALID",
    "R13_CERT_RR_INVALID",
    "R13_CERT_ALPHA_INVALID",
    "R13_CERT_A01_A28_NOT_PASS",
    "R13_CERT_GATE_EVIDENCE_MISSING",
    "R13_CERT_GATE_EVIDENCE_HASH_MISMATCH",
    "R13_CERT_CROSS_BINDING_MISMATCH",
    "R13_CERT_PROTECTED_BOUNDARY_INVALID",
    "R13_CERT_OUTPUT_VALIDATION_FAILED",
)

ROLE_TYPES = {
    "h02_batch_manifest": "OTHER",
    "dataset_manifest": "DATASET_MANIFEST",
    "universe_snapshot": "UNIVERSE_SNAPSHOT",
    "lifecycle_event_set": "LIFECYCLE_EVENT_SET",
    "coverage_record_set": "COVERAGE_RECORD_SET",
    "eligibility_record_set": "ELIGIBILITY_RECORD_SET",
    "pit_certification": "PIT_AUDIT",
    "rr_catalogue": "RULE_CATALOGUE",
    "rr_results": "VALIDATOR_REPORT",
    "alpha_binding": "OTHER",
    "source_receipt_index": "RAW_RECEIPT_INDEX",
    "parent_archive": "OTHER",
    "validator_build": "VALIDATOR_REPORT",
    "protected_boundary_report": "PROTECTED_BOUNDARY_REPORT",
}


class GenuineR13CertificationError(RuntimeError):
    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown genuine R1.3 certification error code: {code}")
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    role: str
    path: Path
    sha256: str
    byte_size: int
    classification: str
    artifact_type: str


@dataclass(frozen=True, slots=True)
class GenuineCertificationResult:
    manifest: dict[str, object]
    output_path: Path
    output_sha256: str
    admission: AdmissionRecord | None


def _json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GenuineR13CertificationError(
            "R13_CERT_JSON_INVALID", f"{path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise GenuineR13CertificationError(
            "R13_CERT_JSON_INVALID", f"{path}: root must be an object"
        )
    return payload


def _sha_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _z(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise GenuineR13CertificationError(
            "R13_CERT_CROSS_BINDING_MISMATCH", f"{field} must be a timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GenuineR13CertificationError(
            "R13_CERT_CROSS_BINDING_MISMATCH", f"{field} is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None:
        raise GenuineR13CertificationError(
            "R13_CERT_CROSS_BINDING_MISMATCH", f"{field} must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _require_sha(value: object, code: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise GenuineR13CertificationError(code, f"{field} must be lowercase SHA-256")
    return value


def _obj(parent: Mapping[str, object], key: str, code: str) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise GenuineR13CertificationError(code, f"{key} must be an object")
    return value


def _list(parent: Mapping[str, object], key: str, code: str) -> list[object]:
    value = parent.get(key)
    if not isinstance(value, list):
        raise GenuineR13CertificationError(code, f"{key} must be an array")
    return value


def _partition_hashes(dataset_manifest: Mapping[str, object]) -> tuple[str, str]:
    partitions = _list(
        dataset_manifest, "partitions", "R13_CERT_CROSS_BINDING_MISMATCH"
    )
    if not partitions:
        raise GenuineR13CertificationError(
            "R13_CERT_CROSS_BINDING_MISMATCH", "dataset partitions cannot be empty"
        )
    hashes: list[str] = []
    for index, item in enumerate(partitions):
        if not isinstance(item, Mapping):
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH",
                f"dataset.partitions[{index}] must be an object",
            )
        hashes.append(
            _require_sha(
                item.get("content_hash"),
                "R13_CERT_CROSS_BINDING_MISMATCH",
                f"dataset.partitions[{index}].content_hash",
            )
        )
    return (
        sha256(canonical_json(hashes)).hexdigest(),
        sha256(canonical_json(partitions)).hexdigest(),
    )


class GenuineR13ManifestAssembler:
    def __init__(self, *, project_root: str | Path | None = None):
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else Path(__file__).resolve().parents[3]
        )

    def _load_index(self, index_path: Path) -> dict[str, object]:
        payload = _json_object(index_path)
        schema = json.loads(INPUT_SCHEMA_PATH.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema, format_checker=FormatChecker())
        errors = sorted(
            validator.iter_errors(payload),
            key=lambda exc: tuple(str(x) for x in exc.absolute_path),
        )
        if errors:
            err = errors[0]
            location = ".".join(str(x) for x in err.absolute_path) or "$"
            raise GenuineR13CertificationError(
                "R13_CERT_INPUT_SCHEMA_INVALID",
                f"{location}: {err.message}",
            )
        return payload

    def _resolve_path(self, raw: object, *, base: Path) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise GenuineR13CertificationError(
                "R13_CERT_REQUIRED_ARTIFACT_MISSING", "artifact path is empty"
            )
        candidate = Path(raw)
        candidates = [candidate] if candidate.is_absolute() else [
            base / candidate,
            self.project_root / candidate,
        ]
        for item in candidates:
            try:
                resolved = item.resolve(strict=True)
            except OSError:
                continue
            if resolved.is_file():
                return resolved
        raise GenuineR13CertificationError(
            "R13_CERT_REQUIRED_ARTIFACT_MISSING", raw
        )


    def _verify_artifacts(
        self, index: Mapping[str, object], *, base: Path
    ) -> dict[str, VerifiedArtifact]:
        descriptors = _obj(
            index, "artifacts", "R13_CERT_INPUT_SCHEMA_INVALID"
        )
        verified: dict[str, VerifiedArtifact] = {}
        for role, expected_type in ROLE_TYPES.items():
            descriptor = _obj(
                descriptors, role, "R13_CERT_INPUT_SCHEMA_INVALID"
            )
            classification = descriptor.get("classification")
            if classification not in {"REAL_MARKET", "DERIVED_REAL_MARKET"}:
                raise GenuineR13CertificationError(
                    "R13_CERT_ARTIFACT_CLASSIFICATION_INVALID",
                    f"{role}: {classification}",
                )
            if descriptor.get("artifact_type") != expected_type:
                raise GenuineR13CertificationError(
                    "R13_CERT_ARTIFACT_TYPE_INVALID",
                    f"{role}: expected {expected_type}",
                )
            path = self._resolve_path(descriptor.get("path"), base=base)
            expected = _require_sha(
                descriptor.get("sha256"),
                "R13_CERT_ARTIFACT_HASH_MISMATCH",
                f"{role}.sha256",
            )
            actual = _sha_file(path)
            if actual != expected:
                raise GenuineR13CertificationError(
                    "R13_CERT_ARTIFACT_HASH_MISMATCH",
                    f"{role}: expected {expected}, got {actual}",
                )
            verified[role] = VerifiedArtifact(
                role=role,
                path=path,
                sha256=actual,
                byte_size=path.stat().st_size,
                classification=str(classification),
                artifact_type=expected_type,
            )
        return verified

    def _verify_gate_evidence(
        self,
        alpha: Mapping[str, object],
        *,
        base: Path,
    ) -> None:
        gates = _obj(alpha, "gates", "R13_CERT_ALPHA_INVALID")
        for gate_id in GATE_IDS:
            gate = _obj(gates, gate_id, "R13_CERT_ALPHA_INVALID")
            if gate.get("status") != "PASS":
                raise GenuineR13CertificationError(
                    "R13_CERT_A01_A28_NOT_PASS", gate_id
                )

            evidence = gate.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                raise GenuineR13CertificationError(
                    "R13_CERT_GATE_EVIDENCE_MISSING",
                    f"{gate_id} has no evidence",
                )
            for item in evidence:
                if not isinstance(item, Mapping):
                    raise GenuineR13CertificationError(
                        "R13_CERT_GATE_EVIDENCE_MISSING",
                        f"{gate_id} evidence entry is invalid",
                    )
                if item.get("classification") == "TEST_FIXTURE":
                    raise GenuineR13CertificationError(
                        "R13_CERT_ARTIFACT_CLASSIFICATION_INVALID",
                        f"{gate_id} references TEST_FIXTURE evidence",
                    )
                path = self._resolve_path(item.get("uri_or_path"), base=base)
                expected = _require_sha(
                    item.get("sha256"),
                    "R13_CERT_GATE_EVIDENCE_HASH_MISMATCH",
                    f"{gate_id}.evidence.sha256",
                )
                actual = _sha_file(path)
                if actual != expected:
                    raise GenuineR13CertificationError(
                        "R13_CERT_GATE_EVIDENCE_HASH_MISMATCH",
                        f"{gate_id}: {path}",
                    )

    def _validate_alpha(
        self,
        artifact: VerifiedArtifact,
        *,
        base: Path,
    ) -> dict[str, object]:
        alpha = _json_object(artifact.path)
        try:
            validate_alpha_data_binding_report(alpha)
        except Exception as exc:
            raise GenuineR13CertificationError(
                "R13_CERT_ALPHA_INVALID", str(exc)
            ) from exc
        if alpha.get("evidence_classification") != "REAL_MARKET":
            raise GenuineR13CertificationError(
                "R13_CERT_ALPHA_INVALID",
                "alpha binding evidence_classification must be REAL_MARKET",
            )
        final = _obj(alpha, "final_decision", "R13_CERT_ALPHA_INVALID")
        if final.get("ALPHA_DATA_BINDING_READY") is not True:
            raise GenuineR13CertificationError(
                "R13_CERT_A01_A28_NOT_PASS",
                "ALPHA_DATA_BINDING_READY is false",
            )
        self._verify_gate_evidence(alpha, base=base)
        return alpha


    def _validate_rr(
        self,
        catalogue_artifact: VerifiedArtifact,
        results_artifact: VerifiedArtifact,
        validator_artifact: VerifiedArtifact,
    ) -> tuple[dict[str, object], dict[str, object]]:
        catalogue = _json_object(catalogue_artifact.path)
        results = _json_object(results_artifact.path)
        rules = catalogue.get("rules")
        if not isinstance(rules, list):
            raise GenuineR13CertificationError(
                "R13_CERT_RR_INVALID", "RR catalogue rules missing"
            )
        catalogue_ids = [
            item.get("rule_id") if isinstance(item, Mapping) else None
            for item in rules
        ]
        if tuple(catalogue_ids) != RR_IDS:
            raise GenuineR13CertificationError(
                "R13_CERT_RR_INVALID",
                "RR catalogue must contain RR-001 through RR-030 exactly once in order",
            )
        if catalogue.get("catalogue_id") != "EQS-H02-RR-CATALOGUE":
            raise GenuineR13CertificationError(
                "R13_CERT_RR_INVALID", "RR catalogue_id mismatch"
            )
        if catalogue.get("catalogue_version") != "RR-001-030-v1":
            raise GenuineR13CertificationError(
                "R13_CERT_RR_INVALID", "RR catalogue_version mismatch"
            )
        if results.get("catalogue_sha256") != catalogue_artifact.sha256:
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH",
                "RR results do not bind to catalogue bytes",
            )
        if results.get("validator_build_sha256") != validator_artifact.sha256:
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH",
                "RR results do not bind to validator build bytes",
            )
        rows = results.get("results")
        if not isinstance(rows, list):
            raise GenuineR13CertificationError(
                "R13_CERT_RR_INVALID", "RR results array missing"
            )

        result_ids = [
            item.get("rule_id") if isinstance(item, Mapping) else None
            for item in rows
        ]
        statuses = [
            item.get("status") if isinstance(item, Mapping) else None
            for item in rows
        ]
        if tuple(result_ids) != RR_IDS or any(status != "PASS" for status in statuses):
            raise GenuineR13CertificationError(
                "R13_CERT_RR_INVALID",
                "RR-001 through RR-030 must each execute exactly once and PASS",
            )
        if results.get("result") != "PASS":
            raise GenuineR13CertificationError(
                "R13_CERT_RR_INVALID", "RR validator aggregate result is not PASS"
            )
        return catalogue, results

    def _validate_h02(
        self,
        artifacts: Mapping[str, VerifiedArtifact],
    ) -> tuple[
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        h02 = _json_object(artifacts["h02_batch_manifest"].path)
        dataset = _json_object(artifacts["dataset_manifest"].path)
        try:
            ResearchDatasetManifest.from_record(dict(dataset))
        except Exception as exc:
            raise GenuineR13CertificationError(
                "R13_CERT_H02_INVALID",
                f"dataset manifest integrity verification failed: {exc}",
            ) from exc
        universe = _json_object(artifacts["universe_snapshot"].path)
        lifecycle = _json_object(artifacts["lifecycle_event_set"].path)
        coverage = _json_object(artifacts["coverage_record_set"].path)
        eligibility = _json_object(artifacts["eligibility_record_set"].path)
        if h02.get("schema_id") != "EQS-H02-BATCH-MANIFEST-v1":
            raise GenuineR13CertificationError(
                "R13_CERT_H02_INVALID", "H02 schema_id mismatch"
            )
        if h02.get("status") != "PASS" or h02.get("source_class") != "GENUINE":
            raise GenuineR13CertificationError(
                "R13_CERT_H02_INVALID", "H02 must be GENUINE and PASS"
            )
        if h02.get("dataset_immutable") is not True:
            raise GenuineR13CertificationError(
                "R13_CERT_H02_INVALID", "H02 dataset_immutable must be true"
            )
        bindings = _obj(h02, "bindings", "R13_CERT_H02_INVALID")

        expected = {
            "dataset_manifest_sha256": artifacts["dataset_manifest"].sha256,
            "universe_snapshot_sha256": artifacts["universe_snapshot"].sha256,
            "lifecycle_event_set_sha256": artifacts["lifecycle_event_set"].sha256,
            "coverage_record_set_sha256": artifacts["coverage_record_set"].sha256,
            "eligibility_record_set_sha256": artifacts["eligibility_record_set"].sha256,
            "pit_certification_sha256": artifacts["pit_certification"].sha256,
            "source_receipt_index_sha256": artifacts["source_receipt_index"].sha256,
            "parent_archive_sha256": artifacts["parent_archive"].sha256,
            "protected_boundary_report_sha256": artifacts["protected_boundary_report"].sha256,
        }
        for field, digest in expected.items():
            if bindings.get(field) != digest:
                raise GenuineR13CertificationError(
                    "R13_CERT_CROSS_BINDING_MISMATCH",
                    f"H02 {field} mismatch",
                )
        for field in (
            "dataset_version",
            "builder_version",
            "feature_definition_hash",
            "coverage_start",
            "coverage_end",
        ):
            if not isinstance(h02.get(field), str) or not str(h02[field]).strip():
                raise GenuineR13CertificationError(
                    "R13_CERT_H02_INVALID", f"H02 {field} is required"
                )
        for field in (
            "feature_definition_hash",
            "dataset_content_hash",
            "partition_index_hash",
            "replay_fingerprint",
        ):
            _require_sha(h02.get(field), "R13_CERT_H02_INVALID", field)
        return h02, dataset, universe, lifecycle, coverage, eligibility

    def _validate_pit(
        self,
        artifact: VerifiedArtifact,
        h02: Mapping[str, object],
    ) -> dict[str, object]:
        pit = _json_object(artifact.path)
        if pit.get("schema_id") != "EQS-R1.3-PIT-CERTIFICATION-v1":
            raise GenuineR13CertificationError(
                "R13_CERT_PIT_INVALID", "PIT schema_id mismatch"
            )
        if pit.get("source_class") != "GENUINE" or pit.get("status") != "PASS":
            raise GenuineR13CertificationError(
                "R13_CERT_PIT_INVALID", "PIT must be GENUINE and PASS"
            )
        bindings = _obj(h02, "bindings", "R13_CERT_H02_INVALID")
        if bindings.get("pit_certification_sha256") != artifact.sha256:
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH",
                "H02 does not bind the PIT certification bytes",
            )

        required_zero = (
            "violations",
            "future_events_detected",
            "future_revisions_detected",
            "backward_availability_adjustments",
            "decision_boundary_crossings",
        )
        if any(pit.get(field) != 0 for field in required_zero):
            raise GenuineR13CertificationError(
                "R13_CERT_PIT_INVALID", "PIT violation counters must all be zero"
            )
        if pit.get("survivorship_bias_detected") is not False:
            raise GenuineR13CertificationError(
                "R13_CERT_PIT_INVALID", "PIT survivorship bias detected"
            )
        basis = _obj(pit, "time_basis", "R13_CERT_PIT_INVALID")
        required_basis = {
            "timezone": "UTC",
            "event_time_semantics": "AS_KNOWN_AT",
            "publication_time_required": True,
            "effective_time_required": True,
            "available_at_required": True,
            "ingestion_time_recorded": True,
            "revision_time_recorded": True,
            "future_visibility_prohibited": True,
        }
        if dict(basis) != required_basis:
            raise GenuineR13CertificationError(
                "R13_CERT_PIT_INVALID", "PIT time_basis is not fail-closed"
            )
        return pit

    def _validate_protected_boundary(
        self,
        alpha: Mapping[str, object],
        artifact: VerifiedArtifact,
    ) -> dict[str, object]:
        report = _json_object(artifact.path)
        boundary = _obj(alpha, "protected_boundary", "R13_CERT_ALPHA_INVALID")
        if boundary.get("evidence_sha256") != artifact.sha256:
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH",
                "Alpha binding does not bind protected-boundary report bytes",
            )
        required = {
            "broker_submission_enabled": False,
            "credentials_accessed": False,
            "live_capital_touched": False,
            "protected_files_changed": 0,
            "eqs06_options_included": False,
        }

        for field, expected in required.items():
            if report.get(field) != expected:
                raise GenuineR13CertificationError(
                    "R13_CERT_PROTECTED_BOUNDARY_INVALID",
                    f"{field} must be {expected!r}",
                )
        for field in (
            "broker_submission_enabled",
            "credentials_accessed",
            "live_capital_touched",
            "protected_files_changed",
        ):
            if boundary.get(field) != report.get(field):
                raise GenuineR13CertificationError(
                    "R13_CERT_CROSS_BINDING_MISMATCH",
                    f"protected boundary mismatch for {field}",
                )
        return report

    def _artifact_inventory(
        self,
        artifacts: Mapping[str, VerifiedArtifact],
    ) -> list[dict[str, object]]:
        return [
            {
                "artifact_id": role,
                "artifact_type": artifact.artifact_type,
                "path_or_uri": str(artifact.path),
                "sha256": artifact.sha256,
                "byte_size": artifact.byte_size,
                "classification": artifact.classification,
            }
            for role, artifact in sorted(artifacts.items())
        ]

    def assemble(self, index_path: str | Path) -> dict[str, object]:
        try:
            index_file = Path(index_path).resolve(strict=True)
        except OSError as exc:
            raise GenuineR13CertificationError(
                "R13_CERT_REQUIRED_ARTIFACT_MISSING",
                f"certification input index unavailable: {index_path}",
            ) from exc
        base = index_file.parent
        index = self._load_index(index_file)
        artifacts = self._verify_artifacts(index, base=base)
        alpha = self._validate_alpha(artifacts["alpha_binding"], base=base)
        h02, dataset_src, universe_src, lifecycle_src, coverage_src, eligibility_src = (
            self._validate_h02(artifacts)
        )
        pit_src = self._validate_pit(artifacts["pit_certification"], h02)
        catalogue, rr = self._validate_rr(
            artifacts["rr_catalogue"],
            artifacts["rr_results"],
            artifacts["validator_build"],
        )
        boundary = self._validate_protected_boundary(
            alpha, artifacts["protected_boundary_report"]
        )

        alpha_dataset = _obj(alpha, "dataset", "R13_CERT_ALPHA_INVALID")
        alpha_feature = _obj(alpha, "feature_engine", "R13_CERT_ALPHA_INVALID")
        alpha_parent = _obj(alpha, "parent", "R13_CERT_ALPHA_INVALID")
        if alpha_parent.get("archive_sha256") != artifacts["parent_archive"].sha256:
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH",
                "Alpha parent archive hash does not match parent_archive bytes",
            )
        if alpha_parent.get("dataset_builder_version") != h02.get("builder_version"):
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH",
                "Alpha dataset_builder_version does not match H02 builder_version",
            )
        if dataset_src.get("format_version") != "crypto-perps-research-v1":
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH", "dataset format mismatch"
            )
        comparisons = {
            "dataset_id": (alpha_dataset.get("dataset_id"), dataset_src.get("dataset_id")),
            "manifest_fingerprint": (
                alpha_dataset.get("manifest_fingerprint"),
                dataset_src.get("manifest_fingerprint"),
            ),
            "universe_fingerprint": (
                alpha_dataset.get("universe_fingerprint"),
                dataset_src.get("universe_fingerprint"),
            ),
            "event_identity_hash": (
                alpha_dataset.get("event_identity_hash"),
                dataset_src.get("event_identity_hash"),
            ),
            "decision_time": (
                _z(alpha_dataset.get("decision_time"), "alpha.dataset.decision_time"),
                _z(dataset_src.get("decision_time"), "dataset.decision_time"),
            ),
            "event_count": (alpha_dataset.get("event_count"), dataset_src.get("event_count")),
            "partition_count": (
                alpha_dataset.get("partition_count"),
                len(_list(dataset_src, "partitions", "R13_CERT_CROSS_BINDING_MISMATCH")),
            ),
        }
        for field, (left, right) in comparisons.items():
            if left != right:
                raise GenuineR13CertificationError(
                    "R13_CERT_CROSS_BINDING_MISMATCH",
                    f"dataset {field} mismatch",
                )
        venues = sorted(
            {
                str(item.get("venue"))
                for item in _list(
                    dataset_src, "partitions", "R13_CERT_CROSS_BINDING_MISMATCH"
                )
                if isinstance(item, Mapping)
            }
        )
        if venues != sorted(str(v) for v in alpha_dataset.get("venues", [])):
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH", "dataset venues mismatch"
            )

        if universe_src.get("universe_fingerprint") != alpha_dataset.get(
            "universe_fingerprint"
        ):
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH", "universe fingerprint mismatch"
            )
        if alpha_feature.get("dataset_manifest_fingerprint") != alpha_dataset.get(
            "manifest_fingerprint"
        ) or alpha_feature.get("universe_fingerprint") != alpha_dataset.get(
            "universe_fingerprint"
        ):
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH", "feature lineage mismatch"
            )

        dataset_content_hash, partition_index_hash = _partition_hashes(dataset_src)
        if h02.get("dataset_content_hash") != dataset_content_hash:
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH", "H02 dataset_content_hash mismatch"
            )
        if h02.get("partition_index_hash") != partition_index_hash:
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH", "H02 partition_index_hash mismatch"
            )
        if h02.get("replay_fingerprint") != alpha_dataset.get("replay_fingerprint"):
            raise GenuineR13CertificationError(
                "R13_CERT_CROSS_BINDING_MISMATCH", "H02 replay_fingerprint mismatch"
            )

        coverage = {
            "schema_id": coverage_src["schema_id"],
            "coverage_record_set_sha256": artifacts["coverage_record_set"].sha256,
            "training_complete": coverage_src["training_complete"],
            "validation_complete": coverage_src["validation_complete"],
            "locked_oos_complete": coverage_src["locked_oos_complete"],
            "records": list(coverage_src["records"]),
        }
        eligibility = {
            "schema_id": eligibility_src["schema_id"],
            "eligibility_record_set_sha256": artifacts["eligibility_record_set"].sha256,
            "record_count": eligibility_src["record_count"],
            "records": list(eligibility_src["records"]),
        }
        universe = {
            "schema_id": universe_src["schema_id"],
            "snapshot_id": universe_src["snapshot_id"],
            "snapshot_sha256": artifacts["universe_snapshot"].sha256,
            "universe_fingerprint": universe_src["universe_fingerprint"],
            "record_count": universe_src["record_count"],
            "history_sha256": universe_src["history_sha256"],
            "point_in_time": universe_src["point_in_time"],
            "survivorship_free": universe_src["survivorship_free"],
        }
        lifecycle = {
            "schema_id": lifecycle_src["schema_id"],
            "schema_version": lifecycle_src["schema_version"],
            "record_count": lifecycle_src["record_count"],
            "event_set_sha256": artifacts["lifecycle_event_set"].sha256,
            "listing_events_present": lifecycle_src["listing_events_present"],
            "delisting_events_present": lifecycle_src["delisting_events_present"],
            "spec_revision_events_present": lifecycle_src["spec_revision_events_present"],
            "source_revision_events_present": lifecycle_src["source_revision_events_present"],
            "continuity_status": lifecycle_src["continuity_status"],
        }

        pit = {
            "status": pit_src["status"],
            "certification_id": pit_src["certification_id"],
            "certification_sha256": artifacts["pit_certification"].sha256,
            "audit_version": pit_src["audit_version"],
            "audit_generated_at": _z(pit_src["audit_generated_at"], "pit.audit_generated_at"),
            "records_checked": pit_src["records_checked"],
            "violations": pit_src["violations"],
            "future_events_detected": pit_src["future_events_detected"],
            "future_revisions_detected": pit_src["future_revisions_detected"],
            "survivorship_bias_detected": pit_src["survivorship_bias_detected"],
            "backward_availability_adjustments": pit_src["backward_availability_adjustments"],
            "decision_boundary_crossings": pit_src["decision_boundary_crossings"],
        }

        gates = _obj(alpha, "gates", "R13_CERT_ALPHA_INVALID")
        a01_a28 = {gate_id: str(_obj(gates, gate_id, "R13_CERT_ALPHA_INVALID")["status"]) for gate_id in GATE_IDS}
        readiness = alpha.get("campaign_readiness")
        if not isinstance(readiness, list):
            raise GenuineR13CertificationError(
                "R13_CERT_ALPHA_INVALID", "campaign_readiness missing"
            )
        locked_oos = all(
            isinstance(item, Mapping) and item.get("locked_oos_status") == "SEALED"
            for item in readiness
        )
        validator_version = rr.get("validator_version")
        if not isinstance(validator_version, str):
            raise GenuineR13CertificationError(
                "R13_CERT_RR_INVALID", "validator_version missing"
            )

        manifest: dict[str, object] = {
            "schema_id": "EQS-R1.3-HISTORICAL-CERTIFICATION-MANIFEST-v1",
            "manifest_id": index["manifest_id"],
            "manifest_generation": index["manifest_generation"],
            "created_at": _z(index["created_at"], "created_at"),
            "source_class": "GENUINE",
            "environment": "RESEARCH",
            "dataset": {
                "dataset_id": alpha_dataset["dataset_id"],
                "dataset_version": h02["dataset_version"],
                "format_version": "crypto-perps-research-v1",
                "builder_version": h02["builder_version"],
                "dataset_manifest_fingerprint": alpha_dataset["manifest_fingerprint"],
                "dataset_content_hash": dataset_content_hash,
                "event_identity_hash": alpha_dataset["event_identity_hash"],
                "replay_fingerprint": alpha_dataset["replay_fingerprint"],
                "partition_index_hash": partition_index_hash,
                "partition_count": alpha_dataset["partition_count"],
                "event_count": alpha_dataset["event_count"],
                "coverage_start": _z(h02["coverage_start"], "h02.coverage_start"),
                "coverage_end": _z(h02["coverage_end"], "h02.coverage_end"),
                "decision_time": _z(alpha_dataset["decision_time"], "dataset.decision_time"),
                "venues": list(alpha_dataset["venues"]),
                "instrument_type": "LINEAR_PERPETUAL",
            },

            "time_basis": dict(_obj(pit_src, "time_basis", "R13_CERT_PIT_INVALID")),
            "pit": pit,
            "universe": universe,
            "lifecycle": lifecycle,
            "coverage": coverage,
            "eligibility": eligibility,
            "feature_binding": {
                "feature_engine_version": alpha_feature["version"],
                "dataset_manifest_fingerprint": alpha_feature["dataset_manifest_fingerprint"],
                "universe_fingerprint": alpha_feature["universe_fingerprint"],
                "feature_run_manifest_fingerprints": list(alpha_feature["run_manifest_fingerprints"]),
                "feature_definition_hash": h02["feature_definition_hash"],
            },
            "validator": {
                "validator_id": rr["validator_id"],
                "validator_version": validator_version,
                "validator_build_sha256": artifacts["validator_build"].sha256,
                "semantic_engine_version": rr["semantic_engine_version"],
                "json_schema_version": "2020-12",
                "run_id": rr["run_id"],
                "run_started_at": _z(rr["run_started_at"], "rr.run_started_at"),
                "run_completed_at": _z(rr["run_completed_at"], "rr.run_completed_at"),
                "result": "PASS",
            },
            "rule_catalogue": {
                "catalogue_id": catalogue["catalogue_id"],
                "catalogue_version": catalogue["catalogue_version"],
                "catalogue_sha256": artifacts["rr_catalogue"].sha256,
                "first_rule": "RR-001",
                "last_rule": "RR-030",
                "rule_count": 30,
                "passed_rule_count": 30,
                "failed_rule_count": 0,
                "blocked_rule_count": 0,
            },

            "certification": {
                "certification_run_id": rr["run_id"],
                "status": "PASSED",
                "a01_a28": a01_a28,
                "all_hard_gates_pass": all(value == "PASS" for value in a01_a28.values()),
                "locked_oos_seal_intact": locked_oos,
                "dataset_immutable": True,
                "manifest_immutable": True,
            },
            "artifact_inventory": self._artifact_inventory(artifacts),
            "provenance": {
                "h02_batch_manifest_sha256": artifacts["h02_batch_manifest"].sha256,
                "universe_snapshot_sha256": artifacts["universe_snapshot"].sha256,
                "lifecycle_event_schema_version": lifecycle["schema_version"],
                "lifecycle_event_set_sha256": artifacts["lifecycle_event_set"].sha256,
                "coverage_record_set_sha256": artifacts["coverage_record_set"].sha256,
                "eligibility_record_set_sha256": artifacts["eligibility_record_set"].sha256,
                "validator_version": validator_version,
                "validator_build_sha256": artifacts["validator_build"].sha256,
                "rule_catalog_version": catalogue["catalogue_version"],
                "rule_catalog_sha256": artifacts["rr_catalogue"].sha256,
                "pit_certification_sha256": artifacts["pit_certification"].sha256,
                "source_receipt_index_sha256": artifacts["source_receipt_index"].sha256,
                "parent_archive_sha256": artifacts["parent_archive"].sha256,
            },
            "protected_boundary": {
                "broker_submission_enabled": False,
                "credentials_accessed": False,
                "live_capital_touched": False,
                "protected_files_changed": 0,
                "eqs06_options_included": False,
                "boundary_report_sha256": artifacts["protected_boundary_report"].sha256,
            },
            "manifest_hash": "0" * 64,
        }
        manifest["manifest_hash"] = manifest_content_hash(manifest)

        try:
            validate_r13_manifest(manifest, artifact_root=self.project_root)
        except Exception as exc:
            raise GenuineR13CertificationError(
                "R13_CERT_OUTPUT_VALIDATION_FAILED", str(exc)
            ) from exc
        return manifest


class GenuineR13CertificationRunner:
    def __init__(self, *, project_root: str | Path | None = None):
        self.assembler = GenuineR13ManifestAssembler(project_root=project_root)

    def run(
        self,
        index_path: str | Path,
        output_path: str | Path,
        *,
        admission_ledger: R13AdmissionLedger | None = None,
        admit: bool = False,
        supersedes_admission_id: str | None = None,
    ) -> GenuineCertificationResult:
        manifest = self.assembler.assemble(index_path)
        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        raw = (
            json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False)
            + "\n"
        ).encode("utf-8")
        output.write_bytes(raw)
        output_hash = sha256(raw).hexdigest()
        output.with_suffix(output.suffix + ".sha256").write_text(
            output_hash + "\n", encoding="ascii"
        )
        admission: AdmissionRecord | None = None
        if admit:
            if admission_ledger is None:
                raise GenuineR13CertificationError(
                    "R13_CERT_OUTPUT_VALIDATION_FAILED",
                    "admit=True requires an R13AdmissionLedger",
                )
            admission = admission_ledger.admit(
                manifest,
                expected_manifest_sha256=str(manifest["manifest_hash"]),
                supersedes_admission_id=supersedes_admission_id,
                artifact_root=self.assembler.project_root,
            )
        return GenuineCertificationResult(
            manifest=manifest,
            output_path=output,
            output_sha256=output_hash,
            admission=admission,
        )
