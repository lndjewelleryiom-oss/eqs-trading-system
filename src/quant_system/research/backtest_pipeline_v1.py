from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Mapping

from .backtest_governance_v1 import verify_record
from .strategy_preregistration_v1 import StrategyPreregistration


class BacktestStage(StrEnum):
    TRAINING = "TRAINING"
    PARAMETER_ROBUSTNESS = "PARAMETER_ROBUSTNESS"
    VALIDATION = "VALIDATION"
    OVERFIT_DEFENCE = "OVERFIT_DEFENCE"
    LOCKED_OOS = "LOCKED_OOS"
    ECONOMIC_STRESS = "ECONOMIC_STRESS"
    REGIME_TEST = "REGIME_TEST"
    PORTFOLIO_CONTRIBUTION = "PORTFOLIO_CONTRIBUTION"
    CERTIFICATION = "CERTIFICATION"
    PAPER_ADMISSION = "PAPER_ADMISSION"


STAGE_ORDER = tuple(BacktestStage)


class PipelineBlocked(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class PipelineBinding:
    lineage_id: str
    preregistration_fingerprint: str
    asset_class: str
    research_policy_sha256: str
    eligibility_matrix_sha256: str


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


def bind_pipeline(
    prereg: StrategyPreregistration,
    *,
    policy: Mapping[str, object],
    matrix: Mapping[str, object],
) -> PipelineBinding:
    if not verify_record(policy) or policy.get("schema_id") != "EQS-BACKTEST-RESEARCH-POLICY-V1":
        raise PipelineBlocked("RESEARCH_POLICY_INVALID", "sealed BT-00 policy is required")
    if not verify_record(matrix) or matrix.get("schema_id") != "EQS-RESEARCH-DATA-ELIGIBILITY-MATRIX-V1":
        raise PipelineBlocked("ELIGIBILITY_MATRIX_INVALID", "sealed BT-01 matrix is required")
    if prereg.research_policy_sha256 != policy["record_sha256"]:
        raise PipelineBlocked("POLICY_BINDING_MISMATCH", "strategy was not preregistered against this policy")
    if prereg.eligibility_matrix_sha256 != matrix["record_sha256"]:
        raise PipelineBlocked("ELIGIBILITY_BINDING_MISMATCH", "strategy was not preregistered against this eligibility matrix")
    rows = [row for row in matrix.get("rows", []) if isinstance(row, dict) and row.get("asset_class") == prereg.asset_class]
    if len(rows) != 1:
        raise PipelineBlocked("ASSET_ELIGIBILITY_ROW_MISSING", prereg.asset_class)
    row = rows[0]
    if row.get("genuine_empirical_research_allowed") is not True:
        raise PipelineBlocked(str(row.get("status", "DATA_NOT_ELIGIBLE")), prereg.asset_class)
    return PipelineBinding(
        lineage_id=prereg.lineage_id,
        preregistration_fingerprint=prereg.fingerprint,
        asset_class=prereg.asset_class,
        research_policy_sha256=str(policy["record_sha256"]),
        eligibility_matrix_sha256=str(matrix["record_sha256"]),
    )


class BacktestPipelineLedger:
    """Append-only stage controller for one or more preregistered strategy lineages.

    This controls research ordering only. It never submits broker orders and cannot
    grant LIVE authority.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS pipeline_bindings(
                    lineage_id TEXT PRIMARY KEY,
                    binding_json TEXT NOT NULL,
                    binding_sha256 TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS pipeline_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    lineage_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    evidence_sha256 TEXT,
                    detail_json TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    prev_hash TEXT,
                    event_hash TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(lineage_id) REFERENCES pipeline_bindings(lineage_id)
                );
                CREATE TRIGGER IF NOT EXISTS pipeline_binding_no_update BEFORE UPDATE ON pipeline_bindings
                BEGIN SELECT RAISE(ABORT,'immutable pipeline binding'); END;
                CREATE TRIGGER IF NOT EXISTS pipeline_binding_no_delete BEFORE DELETE ON pipeline_bindings
                BEGIN SELECT RAISE(ABORT,'immutable pipeline binding'); END;
                CREATE TRIGGER IF NOT EXISTS pipeline_event_no_update BEFORE UPDATE ON pipeline_events
                BEGIN SELECT RAISE(ABORT,'immutable pipeline event'); END;
                CREATE TRIGGER IF NOT EXISTS pipeline_event_no_delete BEFORE DELETE ON pipeline_events
                BEGIN SELECT RAISE(ABORT,'immutable pipeline event'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def create(self, binding: PipelineBinding) -> dict[str, object]:
        payload = {
            "lineage_id": binding.lineage_id,
            "preregistration_fingerprint": binding.preregistration_fingerprint,
            "asset_class": binding.asset_class,
            "research_policy_sha256": binding.research_policy_sha256,
            "eligibility_matrix_sha256": binding.eligibility_matrix_sha256,
            "broker_submission_enabled": False,
            "live_authority": False,
        }
        binding_hash = _hash(payload)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM pipeline_bindings WHERE lineage_id=?", (binding.lineage_id,)).fetchone()
            if row is not None:
                if row["binding_sha256"] != binding_hash:
                    raise PipelineBlocked("PIPELINE_BINDING_IMMUTABLE", binding.lineage_id)
                return dict(row)
            with con:
                con.execute(
                    "INSERT INTO pipeline_bindings VALUES(?,?,?,?)",
                    (binding.lineage_id, json.dumps(payload, sort_keys=True), binding_hash, now),
                )
            return {"lineage_id": binding.lineage_id, "binding_json": json.dumps(payload, sort_keys=True), "binding_sha256": binding_hash, "created_at": now}
        finally:
            con.close()

    def record_stage(
        self,
        lineage_id: str,
        stage: BacktestStage,
        *,
        outcome: str,
        evidence_sha256: str | None,
        detail: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if outcome not in {"PASS", "REJECT", "BLOCKED", "FAILED"}:
            raise ValueError("unsupported stage outcome")
        if outcome == "PASS" and (not isinstance(evidence_sha256, str) or len(evidence_sha256) != 64):
            raise ValueError("PASS requires a SHA-256 evidence binding")
        con = self._connect()
        try:
            if con.execute("SELECT 1 FROM pipeline_bindings WHERE lineage_id=?", (lineage_id,)).fetchone() is None:
                raise PipelineBlocked("PIPELINE_NOT_BOUND", lineage_id)
            existing = con.execute(
                "SELECT * FROM pipeline_events WHERE lineage_id=? ORDER BY seq", (lineage_id,)
            ).fetchall()
            if any(row["outcome"] in {"REJECT", "FAILED"} for row in existing):
                raise PipelineBlocked("PIPELINE_TERMINAL", lineage_id)
            same_stage = [row for row in existing if row["stage"] == stage.value]
            if same_stage:
                raise PipelineBlocked("STAGE_ALREADY_RECORDED", stage.value)
            expected_index = len([row for row in existing if row["outcome"] == "PASS"])
            if expected_index >= len(STAGE_ORDER) or STAGE_ORDER[expected_index] is not stage:
                expected = STAGE_ORDER[expected_index].value if expected_index < len(STAGE_ORDER) else "NONE"
                raise PipelineBlocked("STAGE_ORDER_VIOLATION", f"expected {expected}, received {stage.value}")
            if stage is BacktestStage.LOCKED_OOS:
                opened = con.execute(
                    "SELECT COUNT(*) AS n FROM pipeline_events WHERE lineage_id=? AND stage=?",
                    (lineage_id, BacktestStage.LOCKED_OOS.value),
                ).fetchone()["n"]
                if int(opened) != 0:
                    raise PipelineBlocked("LOCKED_OOS_SINGLE_USE_VIOLATION", lineage_id)

            prev = con.execute("SELECT event_hash FROM pipeline_events ORDER BY seq DESC LIMIT 1").fetchone()
            prev_hash = prev["event_hash"] if prev else None
            now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            payload = {
                "lineage_id": lineage_id,
                "stage": stage.value,
                "outcome": outcome,
                "evidence_sha256": evidence_sha256,
                "detail": dict(detail or {}),
                "event_at": now,
                "prev_hash": prev_hash,
                "broker_submission_enabled": False,
                "live_authority": False,
            }
            event_hash = _hash(payload)
            event_id = f"bt-stage-{event_hash[:20]}"
            with con:
                con.execute(
                    "INSERT INTO pipeline_events(event_id,lineage_id,stage,outcome,evidence_sha256,detail_json,event_at,prev_hash,event_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                    (event_id, lineage_id, stage.value, outcome, evidence_sha256,
                     json.dumps(dict(detail or {}), sort_keys=True), now, prev_hash, event_hash),
                )
            return payload | {"event_id": event_id, "event_hash": event_hash}
        finally:
            con.close()

    def status(self, lineage_id: str) -> dict[str, object]:
        con = self._connect()
        try:
            events = [dict(row) for row in con.execute("SELECT * FROM pipeline_events WHERE lineage_id=? ORDER BY seq", (lineage_id,))]
        finally:
            con.close()
        passed = [row for row in events if row["outcome"] == "PASS"]
        terminal = next((row for row in events if row["outcome"] in {"REJECT", "FAILED"}), None)
        next_stage = None if terminal or len(passed) >= len(STAGE_ORDER) else STAGE_ORDER[len(passed)].value
        return {
            "lineage_id": lineage_id,
            "passed_stage_count": len(passed),
            "next_stage": next_stage,
            "terminal_outcome": None if terminal is None else terminal["outcome"],
            "broker_submission_enabled": False,
            "live_authority": False,
        }

    def verify_hash_chain(self) -> bool:
        con = self._connect()
        try:
            rows = con.execute("SELECT * FROM pipeline_events ORDER BY seq").fetchall()
        finally:
            con.close()
        prev_hash = None
        for row in rows:
            payload = {
                "lineage_id": row["lineage_id"], "stage": row["stage"], "outcome": row["outcome"],
                "evidence_sha256": row["evidence_sha256"], "detail": json.loads(row["detail_json"]),
                "event_at": row["event_at"], "prev_hash": row["prev_hash"],
                "broker_submission_enabled": False, "live_authority": False,
            }
            if row["prev_hash"] != prev_hash or row["event_hash"] != _hash(payload):
                return False
            prev_hash = row["event_hash"]
        return True
