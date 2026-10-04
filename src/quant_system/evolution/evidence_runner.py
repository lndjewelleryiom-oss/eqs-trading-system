from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from quant_system.research.r13_admission import (
    AdmissionRecord,
    R13AdmissionError,
    R13AdmissionLedger,
)

from .evaluator import falsify
from .factory import StrategyBlueprint
from .models import AcceptanceCriteria, ResearchEvidence

LEDGER_SCHEMA_VERSION = "EQS-ALPHA-TRIAL-EVIDENCE-LEDGER-v1"


class SourceClass(StrEnum):
    SYNTHETIC = "SYNTHETIC"
    GENUINE = "GENUINE"


class TrialState(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    PASSED = "PASSED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"


class EvidenceRunnerError(RuntimeError):
    pass


def _canonical(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _sha(payload: object) -> str:
    return sha256(_canonical(payload)).hexdigest()


class AlphaTrialEvidenceLedger:
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
            CREATE TABLE IF NOT EXISTS alpha_ledger_meta(
              key TEXT PRIMARY KEY, value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO alpha_ledger_meta(key,value)
              VALUES('schema_version','EQS-ALPHA-TRIAL-EVIDENCE-LEDGER-v1');
            CREATE TABLE IF NOT EXISTS alpha_trials(
              trial_id TEXT PRIMARY KEY,
              hypothesis_id TEXT NOT NULL,
              source_class TEXT NOT NULL CHECK(source_class IN ('SYNTHETIC','GENUINE')),
              state TEXT NOT NULL,
              created_at TEXT NOT NULL,
              criteria_fingerprint TEXT NOT NULL,
              evaluator_input_hash TEXT NOT NULL,
              blueprint_json TEXT,
              admission_id TEXT,
              admission_generation INTEGER,
              manifest_sha256 TEXT,
              dataset_manifest_fingerprint TEXT,
              universe_fingerprint TEXT,
              h02_batch_manifest_sha256 TEXT,
              universe_snapshot_sha256 TEXT,
              lifecycle_event_schema_version TEXT,
              lifecycle_event_set_sha256 TEXT,
              coverage_record_set_sha256 TEXT,
              eligibility_record_set_sha256 TEXT,
              validator_version TEXT,
              rule_catalog_version TEXT,
              rule_catalog_sha256 TEXT,
              pit_certification_sha256 TEXT,
              lifecycle_error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS alpha_evidence(
              trial_id TEXT PRIMARY KEY,
              evidence_json TEXT NOT NULL,
              evidence_sha256 TEXT NOT NULL,
              decision_reasons_json TEXT NOT NULL,
              FOREIGN KEY(trial_id) REFERENCES alpha_trials(trial_id)
            );
            CREATE TABLE IF NOT EXISTS alpha_journal(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,
              journal_id TEXT NOT NULL UNIQUE,
              trial_id TEXT NOT NULL,
              event_type TEXT NOT NULL,
              event_at TEXT NOT NULL,
              payload_json TEXT NOT NULL,
              prev_hash TEXT,
              journal_hash TEXT NOT NULL UNIQUE
            );
            CREATE TRIGGER IF NOT EXISTS alpha_evidence_no_update
            BEFORE UPDATE ON alpha_evidence BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
            CREATE TRIGGER IF NOT EXISTS alpha_evidence_no_delete
            BEFORE DELETE ON alpha_evidence BEGIN SELECT RAISE(ABORT,'immutable evidence'); END;
            CREATE TRIGGER IF NOT EXISTS alpha_journal_no_update
            BEFORE UPDATE ON alpha_journal BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
            CREATE TRIGGER IF NOT EXISTS alpha_journal_no_delete
            BEFORE DELETE ON alpha_journal BEGIN SELECT RAISE(ABORT,'immutable journal'); END;
            """
        )
        required_columns = {
            "blueprint_json": "TEXT",
            "h02_batch_manifest_sha256": "TEXT",
            "universe_snapshot_sha256": "TEXT",
            "lifecycle_event_schema_version": "TEXT",
            "lifecycle_event_set_sha256": "TEXT",
            "coverage_record_set_sha256": "TEXT",
            "eligibility_record_set_sha256": "TEXT",
            "validator_version": "TEXT",
            "rule_catalog_version": "TEXT",
            "rule_catalog_sha256": "TEXT",
            "pit_certification_sha256": "TEXT",
        }
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(alpha_trials)")}
        for name, column_type in required_columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE alpha_trials ADD COLUMN {name} {column_type}")
        self.db.commit()

    def _journal(self, trial_id: str, event_type: str, payload: dict[str, object]) -> None:
        prev = self.db.execute(
            "SELECT journal_hash FROM alpha_journal ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["journal_hash"] if prev else None
        event_at = datetime.now(timezone.utc).isoformat()
        journal_id = str(uuid4())
        base = {
            "journal_id": journal_id,
            "trial_id": trial_id,
            "event_type": event_type,
            "event_at": event_at,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        self.db.execute(
            """INSERT INTO alpha_journal
            (journal_id,trial_id,event_type,event_at,payload_json,prev_hash,journal_hash)
            VALUES(?,?,?,?,?,?,?)""",
            (
                journal_id, trial_id, event_type, event_at,
                _canonical(payload).decode("utf-8"), prev_hash, _sha(base),
            ),
        )

    def create_trial(
        self,
        *,
        trial_id: str,
        blueprint: StrategyBlueprint,
        source_class: SourceClass,
        criteria: AcceptanceCriteria,
        admission: AdmissionRecord | None,
    ) -> None:
        input_hash = _sha(asdict(blueprint))
        self.db.execute(
            """INSERT INTO alpha_trials(
            trial_id,hypothesis_id,source_class,state,created_at,criteria_fingerprint,evaluator_input_hash,blueprint_json,
            admission_id,admission_generation,manifest_sha256,dataset_manifest_fingerprint,universe_fingerprint,
            h02_batch_manifest_sha256,universe_snapshot_sha256,lifecycle_event_schema_version,
            lifecycle_event_set_sha256,coverage_record_set_sha256,eligibility_record_set_sha256,
            validator_version,rule_catalog_version,rule_catalog_sha256,pit_certification_sha256
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trial_id, blueprint.hypothesis_id, source_class.value, TrialState.CREATED.value,
                datetime.now(timezone.utc).isoformat(), criteria.fingerprint, input_hash,
                _canonical(asdict(blueprint)).decode("utf-8"),
                admission.admission_id if admission else None,
                admission.generation if admission else None,
                admission.manifest.manifest_sha256 if admission else None,
                admission.manifest.dataset_manifest_fingerprint if admission else None,
                admission.manifest.universe_fingerprint if admission else None,
                admission.manifest.h02_batch_manifest_sha256 if admission else None,
                admission.manifest.universe_snapshot_sha256 if admission else None,
                admission.manifest.lifecycle_event_schema_version if admission else None,
                admission.manifest.lifecycle_event_set_sha256 if admission else None,
                admission.manifest.coverage_record_set_sha256 if admission else None,
                admission.manifest.eligibility_record_set_sha256 if admission else None,
                admission.manifest.validator_version if admission else None,
                admission.manifest.rule_catalog_version if admission else None,
                admission.manifest.rule_catalog_sha256 if admission else None,
                admission.manifest.pit_certification_sha256 if admission else None,
            ),
        )
        self._journal(trial_id, "STATE", {"state": TrialState.CREATED.value})
        self.db.commit()

    def transition(self, trial_id: str, state: TrialState, *, error_code: str | None = None) -> None:
        current = self.db.execute(
            "SELECT state FROM alpha_trials WHERE trial_id=?", (trial_id,)
        ).fetchone()
        if current is None:
            raise EvidenceRunnerError(f"unknown trial: {trial_id}")
        allowed = {
            TrialState.CREATED.value: {TrialState.RUNNING.value, TrialState.BLOCKED.value},
            TrialState.RUNNING.value: {TrialState.PASSED.value, TrialState.REJECTED.value, TrialState.BLOCKED.value},
        }
        if state.value not in allowed.get(current["state"], set()):
            raise EvidenceRunnerError(f"illegal trial transition {current['state']} -> {state.value}")
        self.db.execute(
            "UPDATE alpha_trials SET state=?, lifecycle_error_code=? WHERE trial_id=?",
            (state.value, error_code, trial_id),
        )
        self._journal(trial_id, "STATE", {"state": state.value, "error_code": error_code})
        self.db.commit()

    def persist_evidence(
        self, trial_id: str, evidence: ResearchEvidence, decision_reasons: tuple[str, ...]
    ) -> None:
        payload = asdict(evidence)
        self.db.execute(
            """INSERT INTO alpha_evidence
            (trial_id,evidence_json,evidence_sha256,decision_reasons_json)
            VALUES(?,?,?,?)""",
            (
                trial_id,
                _canonical(payload).decode("utf-8"),
                _sha(payload),
                _canonical(decision_reasons).decode("utf-8"),
            ),
        )
        self._journal(
            trial_id, "EVIDENCE",
            {"evidence_sha256": _sha(payload), "decision_reasons": decision_reasons},
        )
        self.db.commit()

    def trial(self, trial_id: str) -> dict[str, object]:
        row = self.db.execute(
            "SELECT * FROM alpha_trials WHERE trial_id=?", (trial_id,)
        ).fetchone()
        if row is None:
            raise EvidenceRunnerError(f"unknown trial: {trial_id}")
        return dict(row)

    def verify_hash_chain(self) -> bool:
        prev_hash: str | None = None
        for row in self.db.execute("SELECT * FROM alpha_journal ORDER BY seq"):
            payload = json.loads(row["payload_json"])
            base = {
                "journal_id": row["journal_id"],
                "trial_id": row["trial_id"],
                "event_type": row["event_type"],
                "event_at": row["event_at"],
                "payload": payload,
                "prev_hash": row["prev_hash"],
            }
            if row["prev_hash"] != prev_hash or row["journal_hash"] != _sha(base):
                return False
            prev_hash = row["journal_hash"]
        return True


class PersistentEvidenceRunner:
    """Fail-closed EvidenceRunner with immutable source-generation provenance."""

    def __init__(
        self,
        evaluator: Callable[[StrategyBlueprint], ResearchEvidence],
        ledger: AlphaTrialEvidenceLedger,
        *,
        source_class: SourceClass = SourceClass.SYNTHETIC,
        admission_ledger: R13AdmissionLedger | None = None,
    ):
        self.evaluator = evaluator
        self.ledger = ledger
        self.source_class = source_class
        self.admission_ledger = admission_ledger
        self.last_trial_id: str | None = None

    def _resolve_admission(self) -> AdmissionRecord | None:
        if self.source_class is SourceClass.SYNTHETIC:
            return None
        if self.admission_ledger is None:
            raise R13AdmissionError("R1_3_NOT_ADMITTED", "genuine runner has no admission ledger")
        return self.admission_ledger.resolve_new_trial()

    def run(
        self, blueprint: StrategyBlueprint, *, criteria: AcceptanceCriteria | None = None
    ) -> ResearchEvidence:
        criteria = criteria or AcceptanceCriteria()
        trial_id = str(uuid4())
        self.last_trial_id = trial_id

        admission: AdmissionRecord | None = None
        gate_error: R13AdmissionError | None = None
        try:
            admission = self._resolve_admission()
        except R13AdmissionError as exc:
            gate_error = exc

        self.ledger.create_trial(
            trial_id=trial_id,
            blueprint=blueprint,
            source_class=self.source_class,
            criteria=criteria,
            admission=admission,
        )
        if gate_error is not None:
            self.ledger.transition(trial_id, TrialState.BLOCKED, error_code=gate_error.code)
            raise gate_error

        self.ledger.transition(trial_id, TrialState.RUNNING)
        try:
            evidence = self.evaluator(blueprint)
            decision = falsify(evidence, criteria)
            self.ledger.persist_evidence(trial_id, evidence, decision.reasons)
            self.ledger.transition(
                trial_id, TrialState.PASSED if decision.passed else TrialState.REJECTED
            )
            return evidence
        except Exception as exc:
            current = self.ledger.trial(trial_id)["state"]
            if current == TrialState.RUNNING.value:
                code = getattr(exc, "code", type(exc).__name__)
                self.ledger.transition(trial_id, TrialState.BLOCKED, error_code=str(code))
            raise
