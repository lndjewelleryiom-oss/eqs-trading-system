from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Callable, Iterable, Mapping

from .backtest_governance_v1 import verify_record
from .strategy_preregistration_v1 import StrategyPreregistration


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class ResearchSupervisorJob:
    job_id: str
    lineage_id: str
    asset_class: str
    preregistration_fingerprint: str
    state: str
    blocker: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class SupervisorExecutionResult:
    result: str
    evidence_ref: str
    reason: str

    def __post_init__(self) -> None:
        if self.result not in {"COMPLETE", "BLOCKED", "FAILED"}:
            raise ValueError("unsupported result")
        if not self.evidence_ref.strip() or not self.reason.strip():
            raise ValueError("evidence_ref and reason are required")


class AutonomousResearchSupervisor:
    """Durable fail-closed queue for preregistered research campaigns.

    The supervisor only admits work whose BT-01 asset row is genuinely eligible.
    It cannot grant PAPER, broker, or LIVE authority; downstream stage ledgers and
    promotion gates remain authoritative.
    """

    def __init__(self, path: str | Path, *, max_attempts: int = 3, max_pending: int = 50):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = int(max_attempts)
        self.max_pending = int(max_pending)
        if not 0 < self.max_attempts <= 10 or self.max_pending <= 0:
            raise ValueError("invalid supervisor limits")
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def _init(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS research_supervisor_jobs(
                    job_id TEXT PRIMARY KEY,
                    lineage_id TEXT NOT NULL UNIQUE,
                    asset_class TEXT NOT NULL,
                    preregistration_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    blocker TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS research_supervisor_journal(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    event_at TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    previous_hash TEXT,
                    event_hash TEXT NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS supervisor_journal_no_update BEFORE UPDATE ON research_supervisor_journal
                BEGIN SELECT RAISE(ABORT,'immutable supervisor journal'); END;
                CREATE TRIGGER IF NOT EXISTS supervisor_journal_no_delete BEFORE DELETE ON research_supervisor_journal
                BEGIN SELECT RAISE(ABORT,'immutable supervisor journal'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def reconcile(self, preregistrations: Iterable[StrategyPreregistration], *, matrix: Mapping[str, object]) -> tuple[ResearchSupervisorJob, ...]:
        if not verify_record(matrix) or matrix.get("schema_id") != "EQS-RESEARCH-DATA-ELIGIBILITY-MATRIX-V1":
            raise ValueError("sealed BT-01 matrix required")
        rows = {str(row["asset_class"]): row for row in matrix.get("rows", []) if isinstance(row, dict) and row.get("asset_class")}
        con = self._connect()
        try:
            pending = int(con.execute("SELECT COUNT(*) n FROM research_supervisor_jobs WHERE state IN ('QUEUED','RUNNING')").fetchone()["n"])
            for prereg in sorted(tuple(preregistrations), key=lambda p: p.lineage_id):
                existing = con.execute("SELECT * FROM research_supervisor_jobs WHERE lineage_id=?", (prereg.lineage_id,)).fetchone()
                row = rows.get(prereg.asset_class)
                eligible = bool(row and row.get("genuine_empirical_research_allowed") is True)
                blocker = None if eligible else str((row or {}).get("status", "ASSET_ELIGIBILITY_ROW_MISSING"))
                desired = "QUEUED" if eligible else "BLOCKED"
                if existing is None:
                    if desired == "QUEUED" and pending >= self.max_pending:
                        desired, blocker = "BLOCKED", "SUPERVISOR_QUEUE_CAPACITY_EXHAUSTED"
                    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    job_id = "research-" + _hash({"lineage": prereg.lineage_id, "fingerprint": prereg.fingerprint})[:20]
                    with con:
                        con.execute(
                            "INSERT INTO research_supervisor_jobs(job_id,lineage_id,asset_class,preregistration_fingerprint,state,blocker,created_at,updated_at,attempts) VALUES(?,?,?,?,?,?,?,?,0)",
                            (job_id, prereg.lineage_id, prereg.asset_class, prereg.fingerprint, desired, blocker, now, now),
                        )
                        self._journal(con, job_id, None, desired, blocker or "ELIGIBLE_GENUINE_RESEARCH_DATA")
                    if desired == "QUEUED": pending += 1
                else:
                    if existing["preregistration_fingerprint"] != prereg.fingerprint:
                        raise ValueError("preregistration fingerprint changed for existing supervisor job")
                    if existing["state"] == "BLOCKED" and eligible and existing["blocker"] != "SUPERVISOR_QUEUE_CAPACITY_EXHAUSTED":
                        if pending < self.max_pending:
                            self._transition(con, existing["job_id"], "QUEUED", None, "DATA_ELIGIBILITY_NOW_PASS")
                            pending += 1
            return self.jobs()
        finally:
            con.close()

    def process_one(self, handler: Callable[[ResearchSupervisorJob], SupervisorExecutionResult]) -> dict[str, object] | None:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM research_supervisor_jobs WHERE state='QUEUED' ORDER BY created_at,job_id LIMIT 1").fetchone()
            if row is None:
                return None
            job = self._decode(row)
            attempts = int(row["attempts"])
            if attempts >= self.max_attempts:
                self._transition(con, job.job_id, "BLOCKED", "ATTEMPT_BUDGET_EXHAUSTED", "ATTEMPT_BUDGET_EXHAUSTED")
                return {"job_id": job.job_id, "result": "BLOCKED", "reason": "ATTEMPT_BUDGET_EXHAUSTED"}
            self._transition(con, job.job_id, "RUNNING", None, "SUPERVISOR_CLAIM")
            with con:
                con.execute("UPDATE research_supervisor_jobs SET attempts=attempts+1 WHERE job_id=?", (job.job_id,))
        finally:
            con.close()

        try:
            result = handler(self.get(job.job_id))
        except Exception as exc:
            result = SupervisorExecutionResult("FAILED", "handler-exception:" + job.job_id, "HANDLER_EXCEPTION:" + type(exc).__name__)

        con = self._connect()
        try:
            current = con.execute("SELECT attempts FROM research_supervisor_jobs WHERE job_id=?", (job.job_id,)).fetchone()
            attempts = int(current["attempts"])
            if result.result == "COMPLETE":
                self._transition(con, job.job_id, "COMPLETE", None, result.reason)
            elif result.result == "BLOCKED":
                self._transition(con, job.job_id, "BLOCKED", result.reason, result.reason)
            elif attempts >= self.max_attempts:
                self._transition(con, job.job_id, "BLOCKED", "ATTEMPT_BUDGET_EXHAUSTED:" + result.reason, result.reason)
            else:
                self._transition(con, job.job_id, "QUEUED", None, "RETRY:" + result.reason)
            return {"job_id":job.job_id,"result":result.result,"reason":result.reason,"evidence_ref":result.evidence_ref,"attempt":attempts,"broker_submission_enabled":False,"live_authority":False}
        finally:
            con.close()

    def _transition(self, con: sqlite3.Connection, job_id: str, target: str, blocker: str | None, reason: str) -> None:
        row = con.execute("SELECT state FROM research_supervisor_jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with con:
            con.execute("UPDATE research_supervisor_jobs SET state=?,blocker=?,updated_at=? WHERE job_id=?", (target, blocker, now, job_id))
            self._journal(con, job_id, row["state"], target, reason)

    def _journal(self, con: sqlite3.Connection, job_id: str, from_state: str | None, to_state: str, reason: str) -> None:
        prev = con.execute("SELECT event_hash FROM research_supervisor_journal ORDER BY seq DESC LIMIT 1").fetchone()
        previous_hash = prev["event_hash"] if prev else None
        event_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        body = {"job_id":job_id,"event_at":event_at,"from_state":from_state,"to_state":to_state,"reason":reason,"previous_hash":previous_hash,"broker_submission_enabled":False,"live_authority":False}
        con.execute("INSERT INTO research_supervisor_journal(job_id,event_at,from_state,to_state,reason,previous_hash,event_hash) VALUES(?,?,?,?,?,?,?)", (job_id,event_at,from_state,to_state,reason,previous_hash,_hash(body)))

    def get(self, job_id: str) -> ResearchSupervisorJob:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM research_supervisor_jobs WHERE job_id=?", (job_id,)).fetchone()
        finally:
            con.close()
        if row is None: raise KeyError(job_id)
        return self._decode(row)

    def jobs(self) -> tuple[ResearchSupervisorJob, ...]:
        con = self._connect()
        try:
            rows = con.execute("SELECT * FROM research_supervisor_jobs ORDER BY created_at,job_id").fetchall()
        finally:
            con.close()
        return tuple(self._decode(row) for row in rows)

    @staticmethod
    def _decode(row: sqlite3.Row) -> ResearchSupervisorJob:
        return ResearchSupervisorJob(job_id=row["job_id"],lineage_id=row["lineage_id"],asset_class=row["asset_class"],preregistration_fingerprint=row["preregistration_fingerprint"],state=row["state"],blocker=row["blocker"],created_at=row["created_at"])

    def verify_hash_chain(self) -> bool:
        con = self._connect()
        try:
            rows = con.execute("SELECT * FROM research_supervisor_journal ORDER BY seq").fetchall()
        finally:
            con.close()
        previous = None
        for row in rows:
            body={"job_id":row["job_id"],"event_at":row["event_at"],"from_state":row["from_state"],"to_state":row["to_state"],"reason":row["reason"],"previous_hash":row["previous_hash"],"broker_submission_enabled":False,"live_authority":False}
            if row["previous_hash"] != previous or row["event_hash"] != _hash(body): return False
            previous=row["event_hash"]
        return True
