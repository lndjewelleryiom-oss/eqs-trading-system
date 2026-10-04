from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Callable

from .replacement import (
    ReplacementRequestState,
    ReplacementResearchQueue,
    ReplacementResearchRequest,
)


@dataclass(frozen=True, slots=True)
class ReplacementExecutionResult:
    result: str
    evidence_ref: str
    reason: str

    def __post_init__(self) -> None:
        if self.result not in {"COMPLETE", "BLOCKED", "FAILED"}:
            raise ValueError("unsupported replacement execution result")
        if not self.evidence_ref.strip():
            raise ValueError("evidence_ref is required")
        if not self.reason.strip():
            raise ValueError("reason is required")


@dataclass(frozen=True, slots=True)
class ReplacementWorkerOutcome:
    request_id: str
    attempt: int
    worker_result: str
    queue_state: str
    evidence_ref: str
    reason: str
    outcome_sha256: str


class ReplacementResearchWorker:
    """Durable bounded consumer for replacement research requests.

    The worker never generates capital authority. A handler may execute bounded
    research mechanics and must return an evidence reference plus explicit result.
    """

    def __init__(
        self,
        queue: ReplacementResearchQueue,
        ledger_path: str | Path,
        *,
        max_attempts: int = 3,
    ):
        self.queue = queue
        self.ledger_path = Path(ledger_path)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = int(max_attempts)
        if not 0 < self.max_attempts <= 10:
            raise ValueError("max_attempts must be in [1,10]")
        self._init_schema()

    def _connect(self):
        con = sqlite3.connect(self.ledger_path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS replacement_worker_attempts(
                    request_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    worker_result TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    outcome_sha256 TEXT NOT NULL UNIQUE,
                    PRIMARY KEY(request_id,attempt)
                );
                CREATE TRIGGER IF NOT EXISTS replacement_worker_attempts_no_update
                BEFORE UPDATE ON replacement_worker_attempts
                BEGIN SELECT RAISE(ABORT,'immutable replacement worker attempt'); END;
                CREATE TRIGGER IF NOT EXISTS replacement_worker_attempts_no_delete
                BEFORE DELETE ON replacement_worker_attempts
                BEGIN SELECT RAISE(ABORT,'immutable replacement worker attempt'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    @staticmethod
    def _canonical(value: object) -> bytes:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")

    def _attempt_count(self, request_id: str) -> int:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT COUNT(*) n FROM replacement_worker_attempts WHERE request_id=?",
                (request_id,),
            ).fetchone()
            return int(row["n"])
        finally:
            con.close()

    def _record(
        self,
        request: ReplacementResearchRequest,
        *,
        attempt: int,
        started_at: str,
        result: ReplacementExecutionResult,
    ) -> ReplacementWorkerOutcome:
        completed_at = datetime.now(timezone.utc).isoformat()
        body = {
            "request_id": request.request_id,
            "attempt": attempt,
            "started_at": started_at,
            "completed_at": completed_at,
            "worker_result": result.result,
            "evidence_ref": result.evidence_ref,
            "reason": result.reason,
            "broker_submission_enabled": False,
            "live_authority": False,
        }
        outcome_sha = sha256(self._canonical(body)).hexdigest()
        con = self._connect()
        try:
            with con:
                con.execute(
                    """INSERT INTO replacement_worker_attempts(
                       request_id,attempt,started_at,completed_at,worker_result,
                       evidence_ref,reason,outcome_sha256
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        request.request_id, attempt, started_at, completed_at,
                        result.result, result.evidence_ref, result.reason, outcome_sha,
                    ),
                )
        finally:
            con.close()

        queue_state = self.queue.get(request.request_id).state.value
        return ReplacementWorkerOutcome(
            request_id=request.request_id,
            attempt=attempt,
            worker_result=result.result,
            queue_state=queue_state,
            evidence_ref=result.evidence_ref,
            reason=result.reason,
            outcome_sha256=outcome_sha,
        )

    def process_one(
        self,
        handler: Callable[[ReplacementResearchRequest], ReplacementExecutionResult],
    ) -> ReplacementWorkerOutcome | None:
        queued = tuple(
            request for request in self.queue.pending()
            if request.state is ReplacementRequestState.QUEUED
        )
        if not queued:
            return None
        request = queued[0]
        completed_attempts = self._attempt_count(request.request_id)
        if completed_attempts >= self.max_attempts:
            self.queue.transition(
                request.request_id,
                ReplacementRequestState.BLOCKED,
                detail="WORKER_ATTEMPT_BUDGET_EXHAUSTED",
            )
            result = ReplacementExecutionResult(
                "BLOCKED",
                "attempt-budget:" + request.request_id,
                "WORKER_ATTEMPT_BUDGET_EXHAUSTED",
            )
            return self._record(
                request,
                attempt=completed_attempts + 1,
                started_at=datetime.now(timezone.utc).isoformat(),
                result=result,
            )

        claimed = self.queue.transition(
            request.request_id,
            ReplacementRequestState.CLAIMED,
            detail="REPLACEMENT_WORKER_CLAIM",
        )
        attempt = completed_attempts + 1
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            result = handler(claimed)
        except Exception as exc:
            result = ReplacementExecutionResult(
                "FAILED",
                "worker-exception:" + request.request_id,
                "HANDLER_EXCEPTION:" + type(exc).__name__,
            )

        if result.result == "COMPLETE":
            self.queue.transition(
                request.request_id,
                ReplacementRequestState.COMPLETED,
                detail="RESEARCH_HANDLER_COMPLETE:" + result.evidence_ref,
            )
        elif result.result == "BLOCKED":
            self.queue.transition(
                request.request_id,
                ReplacementRequestState.BLOCKED,
                detail="RESEARCH_HANDLER_BLOCKED:" + result.reason,
            )
        else:
            if attempt >= self.max_attempts:
                self.queue.transition(
                    request.request_id,
                    ReplacementRequestState.BLOCKED,
                    detail="WORKER_ATTEMPT_BUDGET_EXHAUSTED:" + result.reason,
                )
            else:
                self.queue.transition(
                    request.request_id,
                    ReplacementRequestState.QUEUED,
                    detail="RESEARCH_HANDLER_RETRY:" + result.reason,
                )

        return self._record(
            request,
            attempt=attempt,
            started_at=started_at,
            result=result,
        )

    def attempts(self, request_id: str) -> tuple[dict[str, object], ...]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT * FROM replacement_worker_attempts
                   WHERE request_id=? ORDER BY attempt""",
                (request_id,),
            ).fetchall()
            return tuple(dict(row) for row in rows)
        finally:
            con.close()

    def verify(self) -> bool:
        con = self._connect()
        try:
            for row in con.execute(
                "SELECT * FROM replacement_worker_attempts ORDER BY request_id,attempt"
            ):
                body = {
                    "request_id": row["request_id"],
                    "attempt": int(row["attempt"]),
                    "started_at": row["started_at"],
                    "completed_at": row["completed_at"],
                    "worker_result": row["worker_result"],
                    "evidence_ref": row["evidence_ref"],
                    "reason": row["reason"],
                    "broker_submission_enabled": False,
                    "live_authority": False,
                }
                if sha256(self._canonical(body)).hexdigest() != row["outcome_sha256"]:
                    return False
            return True
        finally:
            con.close()
