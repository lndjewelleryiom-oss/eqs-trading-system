from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from eqs_srf_validator import EQSSRFValidator, ValidationBundle, canonical_sha256
from eqs_srf_validator.engine import canonical_json_bytes


class SRFIntegrationError(RuntimeError):
    pass


class ImmutableEvidenceError(SRFIntegrationError):
    pass


@dataclass(frozen=True, slots=True)
class ArtifactReceipt:
    artifact_type: str
    content_sha256: str
    relative_path: str
    size_bytes: int

    def to_payload(self) -> dict[str, object]:
        return {
            "artifact_type": self.artifact_type,
            "content_sha256": self.content_sha256,
            "relative_path": self.relative_path,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class SRFValidationOutcome:
    bundle: ValidationBundle
    run_manifest: Mapping[str, Any]
    run_manifest_receipt: ArtifactReceipt

    @property
    def aggregate_outcome(self) -> str:
        return str(self.bundle.report["aggregate_outcome"])

    @property
    def passed(self) -> bool:
        return self.aggregate_outcome == "PASS"


class ImmutableEvidenceStore:
    """Content-addressed, append-only JSON evidence store for canonical SRF runs."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.blob_root = self.root / "evidence" / "sha256"
        self.run_root = self.root / "runs"

    @staticmethod
    def _canonical_file_bytes(payload: Any) -> bytes:
        return canonical_json_bytes(payload) + b"\n"

    def _write_exclusive(self, path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ImmutableEvidenceError(f"immutable artifact collision/tamper detected: {path}")
            return
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o444)
        except OSError:
            pass

    def put_json(self, artifact_type: str, payload: Any) -> ArtifactReceipt:
        digest = canonical_sha256(payload)
        data = self._canonical_file_bytes(payload)
        path = self.blob_root / digest[:2] / f"{digest}.json"
        self._write_exclusive(path, data)
        return ArtifactReceipt(artifact_type, digest, str(path.relative_to(self.root)), len(data))

    def put_run_index(self, report_hash: str, name: str, payload: Any) -> ArtifactReceipt:
        digest = canonical_sha256(payload)
        data = self._canonical_file_bytes(payload)
        path = self.run_root / report_hash / name
        self._write_exclusive(path, data)
        return ArtifactReceipt(name, digest, str(path.relative_to(self.root)), len(data))

    def verify_receipt(self, receipt: ArtifactReceipt) -> bool:
        path = self.root / receipt.relative_path
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return canonical_sha256(payload) == receipt.content_sha256

    def verify_content_addressed_tree(self) -> tuple[bool, tuple[str, ...]]:
        failures: list[str] = []
        if not self.blob_root.exists():
            return True, ()
        for path in sorted(self.blob_root.rglob("*.json")):
            expected = path.stem
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                actual = canonical_sha256(payload)
            except Exception as exc:
                failures.append(f"{path}: unreadable:{type(exc).__name__}")
                continue
            if actual != expected:
                failures.append(f"{path}: expected {expected}, got {actual}")
        return not failures, tuple(failures)


class CanonicalSRFResearchWorkflow:
    """Canonical EQS research validation + persistence boundary.

    This validates scientific/research evidence and persists it immutably. It
    does not authorize PAPER or LIVE; those programme gates remain EQS-00 owned.
    """

    def __init__(self, artifact_root: str | Path, validator: EQSSRFValidator | None = None) -> None:
        self.store = ImmutableEvidenceStore(artifact_root)
        self.validator = validator or EQSSRFValidator.from_contract_dir()

    def validate_and_persist(
        self,
        objects: Sequence[Mapping[str, Any]],
        evidence: Mapping[str, Any] | None = None,
        *,
        root_subject_type: str = "EQS-SRF-V1",
        root_subject_id: str = "EQS-SRF-V1-VALIDATION",
        evaluated_at: str | None = None,
        repository_provenance: Mapping[str, Any] | None = None,
    ) -> SRFValidationOutcome:
        bundle = self.validator.validate(
            objects,
            evidence,
            root_subject_type=root_subject_type,
            root_subject_id=root_subject_id,
            evaluated_at=evaluated_at,
        )
        object_receipt = self.store.put_json("canonical_objects", list(objects))
        evidence_receipt = self.store.put_json("runtime_evidence", dict(evidence or {}))
        results_receipt = self.store.put_json("validation_results", bundle.results)
        report_receipt = self.store.put_json("validation_report", bundle.report)
        report_hash = str(bundle.report["report_hash"])

        run_manifest: dict[str, Any] = {
            "object_type": "EQSSRFCanonicalRunManifest",
            "schema_version": "EQS-SRF-CANONICAL-RUN-1.0",
            "framework_version": "EQS-SRF-V1.0",
            "report_hash": report_hash,
            "aggregate_outcome": bundle.report["aggregate_outcome"],
            "visited_catalog_rule_count": bundle.visited_catalog_rule_count,
            "executed_rule_ids_sha256": canonical_sha256(bundle.executed_rule_ids),
            "artifacts": {
                "canonical_objects": object_receipt.to_payload(),
                "runtime_evidence": evidence_receipt.to_payload(),
                "validation_results": results_receipt.to_payload(),
                "validation_report": report_receipt.to_payload(),
            },
            "repository_provenance": dict(repository_provenance or {}),
            "authority_boundary": {
                "research_handoff_only": True,
                "paper_authorized": False,
                "live_authorized": False,
                "programme_gate_authority": "EQS-00",
            },
        }
        run_manifest["manifest_hash"] = canonical_sha256(run_manifest)
        manifest_blob = self.store.put_json("canonical_run_manifest", run_manifest)
        index_payload = {
            "report_hash": report_hash,
            "run_manifest_hash": manifest_blob.content_sha256,
            "run_manifest_path": manifest_blob.relative_path,
        }
        index_receipt = self.store.put_run_index(report_hash, "run_manifest_pointer.json", index_payload)
        return SRFValidationOutcome(bundle, run_manifest, index_receipt)

    def verify_store(self) -> tuple[bool, tuple[str, ...]]:
        return self.store.verify_content_addressed_tree()

    @staticmethod
    def assert_valid_research_handoff(outcome: SRFValidationOutcome) -> None:
        if not outcome.passed:
            raise SRFIntegrationError(f"research qualification is {outcome.aggregate_outcome}, not PASS")
        if outcome.run_manifest["authority_boundary"]["paper_authorized"] is not False:
            raise SRFIntegrationError("research infrastructure cannot authorize PAPER")
        if outcome.run_manifest["authority_boundary"]["live_authorized"] is not False:
            raise SRFIntegrationError("research infrastructure cannot authorize LIVE")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
