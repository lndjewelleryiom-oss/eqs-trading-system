from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Mapping
from uuid import uuid4

from .r13_manifest import (
    R13ManifestSchemaError,
    R13SemanticRuleError,
    SCHEMA_ID as CANONICAL_MANIFEST_SCHEMA_ID,
    validate_canonical_r13_manifest,
)

SCHEMA_VERSION = "EQS-R1.3-ADMISSION-v1"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

ERROR_CODES = (
    "R1_3_MANIFEST_MISSING",
    "R1_3_MANIFEST_INVALID",
    "R1_3_SCHEMA_VERSION_MISMATCH",
    "R1_3_BINDING_NOT_READY",
    "R1_3_GATE_NOT_PASS",
    "R1_3_DATASET_FINGERPRINT_INVALID",
    "R1_3_UNIVERSE_FINGERPRINT_INVALID",
    "R1_3_EVENT_IDENTITY_HASH_INVALID",
    "R1_3_REPLAY_FINGERPRINT_INVALID",
    "R1_3_FEATURE_BINDING_MISMATCH",
    "R1_3_COVERAGE_INCOMPLETE",
    "R1_3_PIT_NOT_PROVEN",
    "R1_3_PROTECTED_BOUNDARY_FAILED",
    "R1_3_MANIFEST_HASH_MISMATCH",
    "R1_3_ADMISSION_CONFLICT",
    "R1_3_GENERATION_STALE",
    "R1_3_ADMISSION_REVOKED",
    "R1_3_ADMISSION_NOT_FOUND",
    "R1_3_REVOCATION_REASON_REQUIRED",
    "R1_3_SUPERSESSION_TARGET_INVALID",
    "R1_3_NOT_ADMITTED",
    *tuple(f"R13_M{i:03d}_FAILED" for i in range(1, 25)),
)


class R13AdmissionError(RuntimeError):
    def __init__(self, code: str, message: str):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown R1.3 admission error code: {code}")
        self.code = code
        super().__init__(f"{code}: {message}")


def canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def payload_sha256(payload: object) -> str:
    return sha256(canonical_json(payload)).hexdigest()
def _require_sha(value: object, code: str, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise R13AdmissionError(code, f"{name} must be lowercase SHA-256")
    return value


def _mapping(parent: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = parent.get(key)
    if not isinstance(value, Mapping):
        raise R13AdmissionError("R1_3_MANIFEST_INVALID", f"{key} must be an object")
    return value


@dataclass(frozen=True, slots=True)
class R13ManifestBinding:
    manifest_sha256: str
    binding_id: str
    dataset_id: str
    dataset_manifest_fingerprint: str
    universe_fingerprint: str
    event_identity_hash: str
    replay_fingerprint: str
    feature_engine_version: str
    feature_run_manifest_fingerprints: tuple[str, ...]
    decision_time: str
    venues: tuple[str, ...]
    h02_batch_manifest_sha256: str
    universe_snapshot_sha256: str
    lifecycle_event_schema_version: str
    lifecycle_event_set_sha256: str
    coverage_record_set_sha256: str
    eligibility_record_set_sha256: str
    validator_version: str
    rule_catalog_version: str
    rule_catalog_sha256: str
    pit_certification_sha256: str


@dataclass(frozen=True, slots=True)
class AdmissionRecord:
    admission_id: str
    generation: int
    admitted_at: str
    manifest: R13ManifestBinding
    manifest_json: str
    supersedes_admission_id: str | None = None


def validate_r13_manifest(
    report: Mapping[str, object],
    *,
    artifact_root: str | Path | None = None,
    prior_manifests: tuple[Mapping[str, object], ...] = (),
) -> R13ManifestBinding:
    if not report:
        raise R13AdmissionError("R1_3_MANIFEST_MISSING", "manifest is empty")
    if report.get("schema_id") != CANONICAL_MANIFEST_SCHEMA_ID:
        raise R13AdmissionError(
            "R1_3_SCHEMA_VERSION_MISMATCH",
            f"expected {CANONICAL_MANIFEST_SCHEMA_ID}",
        )
    try:
        validate_canonical_r13_manifest(
            report,
            artifact_root=artifact_root,
            prior_manifests=prior_manifests,
        )
    except R13ManifestSchemaError as exc:
        if exc.path in {"provenance.rule_catalog_version", "rule_catalogue.catalogue_version"}:
            raise R13AdmissionError("R13_M012_FAILED", str(exc)) from exc
        raise R13AdmissionError("R1_3_MANIFEST_INVALID", str(exc)) from exc
    except R13SemanticRuleError as exc:
        code = f"{exc.rule_id.replace('-', '_')}_FAILED"
        raise R13AdmissionError(code, str(exc)) from exc

    dataset = _mapping(report, "dataset")
    universe = _mapping(report, "universe")
    feature = _mapping(report, "feature_binding")
    provenance = _mapping(report, "provenance")
    run_fps = feature["feature_run_manifest_fingerprints"]

    return R13ManifestBinding(
        manifest_sha256=str(report["manifest_hash"]),
        binding_id=str(report["manifest_hash"]),
        dataset_id=str(dataset["dataset_id"]),
        dataset_manifest_fingerprint=str(dataset["dataset_manifest_fingerprint"]),
        universe_fingerprint=str(universe["universe_fingerprint"]),
        event_identity_hash=str(dataset["event_identity_hash"]),
        replay_fingerprint=str(dataset["replay_fingerprint"]),
        feature_engine_version=str(feature["feature_engine_version"]),
        feature_run_manifest_fingerprints=tuple(str(value) for value in run_fps),
        decision_time=str(dataset["decision_time"]),
        venues=tuple(str(value) for value in dataset["venues"]),
        h02_batch_manifest_sha256=str(provenance["h02_batch_manifest_sha256"]),
        universe_snapshot_sha256=str(provenance["universe_snapshot_sha256"]),
        lifecycle_event_schema_version=str(provenance["lifecycle_event_schema_version"]),
        lifecycle_event_set_sha256=str(provenance["lifecycle_event_set_sha256"]),
        coverage_record_set_sha256=str(provenance["coverage_record_set_sha256"]),
        eligibility_record_set_sha256=str(provenance["eligibility_record_set_sha256"]),
        validator_version=str(provenance["validator_version"]),
        rule_catalog_version=str(provenance["rule_catalog_version"]),
        rule_catalog_sha256=str(provenance["rule_catalog_sha256"]),
        pit_certification_sha256=str(provenance["pit_certification_sha256"]),
    )


class R13AdmissionLedger:
    """Immutable admission facts plus append-only lifecycle events."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.db.close()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS r13_admissions(
              admission_id TEXT PRIMARY KEY,
              generation INTEGER NOT NULL UNIQUE,
              admitted_at TEXT NOT NULL,
              manifest_sha256 TEXT NOT NULL UNIQUE,
              binding_id TEXT NOT NULL,
              dataset_id TEXT NOT NULL,
              dataset_manifest_fingerprint TEXT NOT NULL,
              universe_fingerprint TEXT NOT NULL,
              event_identity_hash TEXT NOT NULL,
              replay_fingerprint TEXT NOT NULL,
              feature_engine_version TEXT NOT NULL,
              feature_run_manifest_fingerprints_json TEXT NOT NULL,
              decision_time TEXT NOT NULL,
              venues_json TEXT NOT NULL,
              h02_batch_manifest_sha256 TEXT NOT NULL,
              universe_snapshot_sha256 TEXT NOT NULL,
              lifecycle_event_schema_version TEXT NOT NULL,
              lifecycle_event_set_sha256 TEXT NOT NULL,
              coverage_record_set_sha256 TEXT NOT NULL,
              eligibility_record_set_sha256 TEXT NOT NULL,
              validator_version TEXT NOT NULL,
              rule_catalog_version TEXT NOT NULL,
              rule_catalog_sha256 TEXT NOT NULL,
              pit_certification_sha256 TEXT NOT NULL,
              manifest_json TEXT NOT NULL,
              supersedes_admission_id TEXT
            );
            CREATE TABLE IF NOT EXISTS r13_admission_events(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              event_id TEXT NOT NULL UNIQUE,
              admission_id TEXT NOT NULL,
              event_type TEXT NOT NULL CHECK(event_type IN ('ADMITTED','SUPERSEDED','REVOKED')),
              occurred_at TEXT NOT NULL,
              reason TEXT NOT NULL,
              related_admission_id TEXT,
              prev_hash TEXT,
              event_hash TEXT NOT NULL UNIQUE
            );
            CREATE TRIGGER IF NOT EXISTS r13_admissions_no_update
            BEFORE UPDATE ON r13_admissions BEGIN SELECT RAISE(ABORT,'immutable admission row'); END;
            CREATE TRIGGER IF NOT EXISTS r13_admissions_no_delete
            BEFORE DELETE ON r13_admissions BEGIN SELECT RAISE(ABORT,'immutable admission row'); END;
            CREATE TRIGGER IF NOT EXISTS r13_events_no_update
            BEFORE UPDATE ON r13_admission_events BEGIN SELECT RAISE(ABORT,'immutable lifecycle event'); END;
            CREATE TRIGGER IF NOT EXISTS r13_events_no_delete
            BEFORE DELETE ON r13_admission_events BEGIN SELECT RAISE(ABORT,'immutable lifecycle event'); END;
            """
        )
        self.db.commit()

    def _latest_event(self, admission_id: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM r13_admission_events WHERE admission_id=? ORDER BY seq DESC LIMIT 1",
            (admission_id,),
        ).fetchone()

    def _active_row(self) -> sqlite3.Row | None:
        rows = self.db.execute("SELECT admission_id FROM r13_admissions ORDER BY generation DESC").fetchall()
        for row in rows:
            event = self._latest_event(row["admission_id"])
            if event and event["event_type"] == "ADMITTED":
                return self.db.execute(
                    "SELECT * FROM r13_admissions WHERE admission_id=?", (row["admission_id"],)
                ).fetchone()
        return None

    def _append_event(
        self, admission_id: str, event_type: str, reason: str, related_admission_id: str | None = None
    ) -> None:
        prev = self.db.execute(
            "SELECT event_hash FROM r13_admission_events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["event_hash"] if prev else None
        event_id = str(uuid4())
        occurred_at = datetime.now(timezone.utc).isoformat()
        payload = {
            "event_id": event_id,
            "admission_id": admission_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "reason": reason,
            "related_admission_id": related_admission_id,
            "prev_hash": prev_hash,
        }
        event_hash = payload_sha256(payload)
        self.db.execute(
            """INSERT INTO r13_admission_events
            (event_id,admission_id,event_type,occurred_at,reason,related_admission_id,prev_hash,event_hash)
            VALUES(?,?,?,?,?,?,?,?)""",
            (event_id, admission_id, event_type, occurred_at, reason, related_admission_id, prev_hash, event_hash),
        )

    def admit(
        self,
        report: Mapping[str, object],
        *,
        expected_manifest_sha256: str | None = None,
        supersedes_admission_id: str | None = None,
        artifact_root: str | Path | None = None,
    ) -> AdmissionRecord:
        root = Path(artifact_root) if artifact_root is not None else self.path.parent
        claimed_hash = report.get("manifest_hash")
        duplicate = None
        if isinstance(claimed_hash, str):
            duplicate = self.db.execute(
                "SELECT admission_id FROM r13_admissions WHERE manifest_sha256=?",
                (claimed_hash,),
            ).fetchone()

        if duplicate:
            binding = validate_r13_manifest(report, artifact_root=root)
            if expected_manifest_sha256 is not None and expected_manifest_sha256 != binding.manifest_sha256:
                raise R13AdmissionError("R1_3_MANIFEST_HASH_MISMATCH", "manifest hash differs from expected")
            return self.get(duplicate["admission_id"])

        prior_rows = self.db.execute(
            "SELECT manifest_json FROM r13_admissions ORDER BY generation"
        ).fetchall()
        prior_manifests = tuple(json.loads(row["manifest_json"]) for row in prior_rows)
        binding = validate_r13_manifest(
            report,
            artifact_root=root,
            prior_manifests=prior_manifests,
        )
        if expected_manifest_sha256 is not None and expected_manifest_sha256 != binding.manifest_sha256:
            raise R13AdmissionError("R1_3_MANIFEST_HASH_MISMATCH", "manifest hash differs from expected")

        active = self._active_row()
        if active is not None:
            if supersedes_admission_id != active["admission_id"]:
                raise R13AdmissionError(
                    "R1_3_ADMISSION_CONFLICT",
                    "an active admission exists; explicit supersession is required",
                )
        elif supersedes_admission_id is not None:
            raise R13AdmissionError(
                "R1_3_SUPERSESSION_TARGET_INVALID", "no active admission exists to supersede"
            )

        generation = int(self.db.execute(
            "SELECT COALESCE(MAX(generation),0)+1 AS n FROM r13_admissions"
        ).fetchone()["n"])
        admission_id = f"r13-admission-{generation:06d}-{binding.manifest_sha256[:12]}"
        admitted_at = datetime.now(timezone.utc).isoformat()
        manifest_json = canonical_json(report).decode("utf-8")
        self.db.execute(
            """INSERT INTO r13_admissions(
            admission_id,generation,admitted_at,manifest_sha256,binding_id,dataset_id,
            dataset_manifest_fingerprint,universe_fingerprint,event_identity_hash,replay_fingerprint,
            feature_engine_version,feature_run_manifest_fingerprints_json,decision_time,venues_json,
            h02_batch_manifest_sha256,universe_snapshot_sha256,lifecycle_event_schema_version,
            lifecycle_event_set_sha256,coverage_record_set_sha256,eligibility_record_set_sha256,
            validator_version,rule_catalog_version,rule_catalog_sha256,pit_certification_sha256,
            manifest_json,supersedes_admission_id
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                admission_id, generation, admitted_at, binding.manifest_sha256, binding.binding_id,
                binding.dataset_id, binding.dataset_manifest_fingerprint, binding.universe_fingerprint,
                binding.event_identity_hash, binding.replay_fingerprint, binding.feature_engine_version,
                json.dumps(binding.feature_run_manifest_fingerprints),
                binding.decision_time, json.dumps(binding.venues),
                binding.h02_batch_manifest_sha256, binding.universe_snapshot_sha256,
                binding.lifecycle_event_schema_version, binding.lifecycle_event_set_sha256,
                binding.coverage_record_set_sha256, binding.eligibility_record_set_sha256,
                binding.validator_version, binding.rule_catalog_version, binding.rule_catalog_sha256,
                binding.pit_certification_sha256, manifest_json, supersedes_admission_id,
            ),
        )
        if active is not None:
            self._append_event(
                active["admission_id"], "SUPERSEDED", "superseded by newer admitted generation", admission_id
            )
        self._append_event(admission_id, "ADMITTED", "validated R1.3 manifest admitted", supersedes_admission_id)
        self.db.commit()
        return self.get(admission_id)

    def revoke(self, admission_id: str, *, reason: str) -> None:
        if not reason.strip():
            raise R13AdmissionError("R1_3_REVOCATION_REASON_REQUIRED", "revocation reason is required")
        row = self.db.execute(
            "SELECT admission_id FROM r13_admissions WHERE admission_id=?", (admission_id,)
        ).fetchone()
        if row is None:
            raise R13AdmissionError("R1_3_ADMISSION_NOT_FOUND", admission_id)
        latest = self._latest_event(admission_id)
        if latest and latest["event_type"] == "REVOKED":
            return
        self._append_event(admission_id, "REVOKED", reason)
        self.db.commit()

    def status(self, admission_id: str) -> str:
        row = self.db.execute(
            "SELECT admission_id FROM r13_admissions WHERE admission_id=?", (admission_id,)
        ).fetchone()
        if row is None:
            raise R13AdmissionError("R1_3_ADMISSION_NOT_FOUND", admission_id)
        latest = self._latest_event(admission_id)
        if latest is None:
            raise R13AdmissionError("R1_3_NOT_ADMITTED", admission_id)
        return latest["event_type"]

    def active(self) -> AdmissionRecord:
        row = self._active_row()
        if row is None:
            raise R13AdmissionError("R1_3_NOT_ADMITTED", "no active genuine R1.3 admission")
        return self._row_to_record(row)

    def get(self, admission_id: str) -> AdmissionRecord:
        row = self.db.execute(
            "SELECT * FROM r13_admissions WHERE admission_id=?", (admission_id,)
        ).fetchone()
        if row is None:
            raise R13AdmissionError("R1_3_ADMISSION_NOT_FOUND", admission_id)
        return self._row_to_record(row)
    def resolve_new_trial(self, *, generation: int | None = None) -> AdmissionRecord:
        record = self.active()
        if generation is not None and record.generation != generation:
            raise R13AdmissionError("R1_3_GENERATION_STALE", f"active={record.generation} requested={generation}")
        return record

    def resolve_pinned_trial(self, admission_id: str, generation: int) -> AdmissionRecord:
        record = self.get(admission_id)
        if record.generation != generation:
            raise R13AdmissionError("R1_3_GENERATION_STALE", "pinned generation does not match admission")
        # Existing trials stay pinned across supersession or later revocation.
        return record

    def verify_hash_chain(self) -> bool:
        prev_hash: str | None = None
        for row in self.db.execute("SELECT * FROM r13_admission_events ORDER BY seq"):
            payload = {
                "event_id": row["event_id"],
                "admission_id": row["admission_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "reason": row["reason"],
                "related_admission_id": row["related_admission_id"],
                "prev_hash": row["prev_hash"],
            }
            if row["prev_hash"] != prev_hash or row["event_hash"] != payload_sha256(payload):
                return False
            prev_hash = row["event_hash"]
        return True

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> AdmissionRecord:
        binding = R13ManifestBinding(
            manifest_sha256=row["manifest_sha256"],
            binding_id=row["binding_id"],
            dataset_id=row["dataset_id"],
            dataset_manifest_fingerprint=row["dataset_manifest_fingerprint"],
            universe_fingerprint=row["universe_fingerprint"],
            event_identity_hash=row["event_identity_hash"],
            replay_fingerprint=row["replay_fingerprint"],
            feature_engine_version=row["feature_engine_version"],
            feature_run_manifest_fingerprints=tuple(json.loads(row["feature_run_manifest_fingerprints_json"])),
            decision_time=row["decision_time"],
            venues=tuple(json.loads(row["venues_json"])),
            h02_batch_manifest_sha256=row["h02_batch_manifest_sha256"],
            universe_snapshot_sha256=row["universe_snapshot_sha256"],
            lifecycle_event_schema_version=row["lifecycle_event_schema_version"],
            lifecycle_event_set_sha256=row["lifecycle_event_set_sha256"],
            coverage_record_set_sha256=row["coverage_record_set_sha256"],
            eligibility_record_set_sha256=row["eligibility_record_set_sha256"],
            validator_version=row["validator_version"],
            rule_catalog_version=row["rule_catalog_version"],
            rule_catalog_sha256=row["rule_catalog_sha256"],
            pit_certification_sha256=row["pit_certification_sha256"],
        )
        return AdmissionRecord(
            admission_id=row["admission_id"],
            generation=int(row["generation"]),
            admitted_at=row["admitted_at"],
            manifest=binding,
            manifest_json=row["manifest_json"],
            supersedes_admission_id=row["supersedes_admission_id"],
        )
