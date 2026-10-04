from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .lifecycle import PersistentStrategyLifecycle, StrategyLifecycleState


class ReplacementRequestState(StrEnum):
    QUEUED = "QUEUED"
    CLAIMED = "CLAIMED"
    COMPLETED = "COMPLETED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


class ReplacementResearchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReplacementResearchRequest:
    request_id: str
    replaced_strategy_id: str
    replaced_strategy_version: str
    asset_class: str
    family: str
    trigger_state: str
    reason: str
    max_hypotheses: int
    state: ReplacementRequestState
    created_at: str
    request_sha256: str


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ReplacementResearchQueue:
    """Persistent bounded queue for replacement research.

    Requests create research work only. They never promote a strategy, allocate
    capital, or grant LIVE authority.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        max_pending: int = 20,
        max_hypotheses_per_request: int = 5,
    ):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_pending = int(max_pending)
        self.max_hypotheses_per_request = int(max_hypotheses_per_request)
        if self.max_pending <= 0 or self.max_hypotheses_per_request <= 0:
            raise ValueError("queue limits must be positive")
        self._init_schema()

    def _connect(self):
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        return con

    def _init_schema(self):
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS replacement_requests(
                    request_id TEXT PRIMARY KEY,
                    replaced_strategy_id TEXT NOT NULL,
                    replaced_strategy_version TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    family TEXT NOT NULL,
                    trigger_state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    max_hypotheses INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    request_sha256 TEXT NOT NULL UNIQUE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_open_replacement_per_strategy
                ON replacement_requests(replaced_strategy_id,replaced_strategy_version)
                WHERE state IN ('QUEUED','CLAIMED','BLOCKED');

                CREATE TABLE IF NOT EXISTS replacement_journal(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT,
                    event_sha256 TEXT NOT NULL UNIQUE
                );

                CREATE TRIGGER IF NOT EXISTS replacement_journal_no_update
                BEFORE UPDATE ON replacement_journal
                BEGIN SELECT RAISE(ABORT,'immutable replacement journal'); END;
                CREATE TRIGGER IF NOT EXISTS replacement_journal_no_delete
                BEFORE DELETE ON replacement_journal
                BEGIN SELECT RAISE(ABORT,'immutable replacement journal'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def _journal(self, con, request_id, from_state, to_state, detail):
        prev = con.execute(
            "SELECT event_sha256 FROM replacement_journal ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = prev["event_sha256"] if prev else None
        occurred_at = datetime.now(timezone.utc).isoformat()
        body = {
            "request_id": request_id,
            "from_state": from_state,
            "to_state": to_state,
            "occurred_at": occurred_at,
            "detail": detail,
            "previous_hash": previous_hash,
        }
        event_sha = sha256(_canonical(body)).hexdigest()
        con.execute(
            """INSERT INTO replacement_journal(
               request_id,from_state,to_state,occurred_at,detail,previous_hash,event_sha256
               ) VALUES(?,?,?,?,?,?,?)""",
            (request_id, from_state, to_state, occurred_at, detail, previous_hash, event_sha),
        )

    def enqueue_for_strategy(
        self,
        *,
        lifecycle: PersistentStrategyLifecycle,
        strategy_id: str,
        strategy_version: str,
        reason: str,
        max_hypotheses: int = 3,
    ) -> ReplacementResearchRequest:
        current = lifecycle.get(strategy_id, strategy_version)
        if not current.asset_class or not current.family:
            raise ReplacementResearchError("STRATEGY_RESEARCH_METADATA_MISSING")
        return self.enqueue(
            lifecycle=lifecycle,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            asset_class=current.asset_class,
            family=current.family,
            reason=reason,
            max_hypotheses=max_hypotheses,
        )

    def enqueue(
        self,
        *,
        lifecycle: PersistentStrategyLifecycle,
        strategy_id: str,
        strategy_version: str,
        asset_class: str,
        family: str,
        reason: str,
        max_hypotheses: int = 3,
    ) -> ReplacementResearchRequest:
        current = lifecycle.get(strategy_id, strategy_version)
        if current.state not in {StrategyLifecycleState.PAUSED, StrategyLifecycleState.RETIRED}:
            raise ReplacementResearchError("REPLACEMENT_REQUIRES_PAUSED_OR_RETIRED_STRATEGY")
        if not asset_class.strip() or not family.strip() or not reason.strip():
            raise ReplacementResearchError("REPLACEMENT_METADATA_REQUIRED")
        max_hypotheses = int(max_hypotheses)
        if not 0 < max_hypotheses <= self.max_hypotheses_per_request:
            raise ReplacementResearchError("REPLACEMENT_HYPOTHESIS_BUDGET_OUT_OF_BOUNDS")

        con = self._connect()
        try:
            pending = con.execute(
                "SELECT COUNT(*) n FROM replacement_requests WHERE state IN ('QUEUED','CLAIMED','BLOCKED')"
            ).fetchone()["n"]
            if int(pending) >= self.max_pending:
                raise ReplacementResearchError("REPLACEMENT_QUEUE_CAPACITY_EXHAUSTED")
            existing = con.execute(
                """SELECT * FROM replacement_requests
                   WHERE replaced_strategy_id=? AND replaced_strategy_version=?
                   AND state IN ('QUEUED','CLAIMED','BLOCKED')""",
                (strategy_id, strategy_version),
            ).fetchone()
            if existing is not None:
                return self._decode(existing)

            request_id = str(uuid4())
            created_at = datetime.now(timezone.utc).isoformat()
            body = {
                "request_id": request_id,
                "replaced_strategy_id": strategy_id,
                "replaced_strategy_version": strategy_version,
                "asset_class": asset_class,
                "family": family,
                "trigger_state": current.state.value,
                "reason": reason,
                "max_hypotheses": max_hypotheses,
                "created_at": created_at,
            }
            request_sha = sha256(_canonical(body)).hexdigest()
            with con:
                con.execute(
                    """INSERT INTO replacement_requests(
                       request_id,replaced_strategy_id,replaced_strategy_version,
                       asset_class,family,trigger_state,reason,max_hypotheses,state,
                       created_at,updated_at,request_sha256
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        request_id, strategy_id, strategy_version, asset_class, family,
                        current.state.value, reason, max_hypotheses,
                        ReplacementRequestState.QUEUED.value, created_at, created_at, request_sha,
                    ),
                )
                self._journal(
                    con, request_id, None, ReplacementRequestState.QUEUED.value,
                    "AUTOMATIC_REPLACEMENT_RESEARCH_TRIGGER",
                )
            return self.get(request_id)
        finally:
            con.close()

    def transition(
        self,
        request_id: str,
        target: ReplacementRequestState,
        *,
        detail: str,
    ) -> ReplacementResearchRequest:
        allowed = {
            ReplacementRequestState.QUEUED: {
                ReplacementRequestState.CLAIMED,
                ReplacementRequestState.BLOCKED,
                ReplacementRequestState.CANCELLED,
            },
            ReplacementRequestState.CLAIMED: {
                ReplacementRequestState.COMPLETED,
                ReplacementRequestState.BLOCKED,
                ReplacementRequestState.QUEUED,
            },
            ReplacementRequestState.BLOCKED: {
                ReplacementRequestState.QUEUED,
                ReplacementRequestState.CANCELLED,
            },
            ReplacementRequestState.COMPLETED: set(),
            ReplacementRequestState.CANCELLED: set(),
        }
        current = self.get(request_id)
        if target not in allowed[current.state]:
            raise ReplacementResearchError(
                f"ILLEGAL_REPLACEMENT_TRANSITION:{current.state.value}->{target.value}"
            )
        con = self._connect()
        try:
            now = datetime.now(timezone.utc).isoformat()
            with con:
                con.execute(
                    "UPDATE replacement_requests SET state=?,updated_at=? WHERE request_id=?",
                    (target.value, now, request_id),
                )
                self._journal(con, request_id, current.state.value, target.value, detail)
            return self.get(request_id)
        finally:
            con.close()

    def get(self, request_id: str) -> ReplacementResearchRequest:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM replacement_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                raise ReplacementResearchError("UNKNOWN_REPLACEMENT_REQUEST")
            return self._decode(row)
        finally:
            con.close()

    def pending(self) -> tuple[ReplacementResearchRequest, ...]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT * FROM replacement_requests
                   WHERE state IN ('QUEUED','CLAIMED','BLOCKED')
                   ORDER BY created_at,request_id"""
            ).fetchall()
            return tuple(self._decode(row) for row in rows)
        finally:
            con.close()

    @staticmethod
    def _decode(row) -> ReplacementResearchRequest:
        return ReplacementResearchRequest(
            request_id=row["request_id"],
            replaced_strategy_id=row["replaced_strategy_id"],
            replaced_strategy_version=row["replaced_strategy_version"],
            asset_class=row["asset_class"],
            family=row["family"],
            trigger_state=row["trigger_state"],
            reason=row["reason"],
            max_hypotheses=int(row["max_hypotheses"]),
            state=ReplacementRequestState(row["state"]),
            created_at=row["created_at"],
            request_sha256=row["request_sha256"],
        )

    def verify_hash_chain(self) -> bool:
        con = self._connect()
        try:
            previous = None
            for row in con.execute("SELECT * FROM replacement_journal ORDER BY sequence"):
                body = {
                    "request_id": row["request_id"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "occurred_at": row["occurred_at"],
                    "detail": row["detail"],
                    "previous_hash": row["previous_hash"],
                }
                if row["previous_hash"] != previous or row["event_sha256"] != sha256(_canonical(body)).hexdigest():
                    return False
                previous = row["event_sha256"]
            return True
        finally:
            con.close()
