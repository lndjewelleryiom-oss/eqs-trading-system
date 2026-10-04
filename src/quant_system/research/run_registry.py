from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


ALLOWED_MODES = {"RESEARCH_REPLAY", "INTERNAL_PAPER", "BROKER_DEMO", "LIVE"}
ALLOWED_VISIBILITY_CLASSES = {"VISIBLE", "SEALED_OOS_OPERATIONAL_ONLY"}
ALLOWED_EVENT_TYPES = {
    "RUN_STARTED", "RUN_PROGRESS", "MARKET_BAR", "MARKET_TICK", "SIGNAL",
    "RISK_DECISION", "ORDER_SUBMITTED", "ORDER_ACK", "ORDER_REJECT",
    "FILL", "PARTIAL_FILL", "POSITION_CHANGE", "TRADE_CLOSED",
    "EQUITY_UPDATE", "HEARTBEAT", "RUN_BLOCKED", "RUN_FAILED",
    "RUN_PAUSED", "RUN_FINISHED",
}
TERMINAL_STATUSES = {"BLOCKED", "FAILED", "CANCELLED", "FINISHED"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class StrategyRunRegistry:
    """Small append-only research/run registry kept separate from the trading runtime DB."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._connect() as c:
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_runs (
                    run_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    market_start_utc TEXT,
                    market_end_utc TEXT,
                    dataset_hash TEXT,
                    code_hash TEXT,
                    config_hash TEXT,
                    parameter_json TEXT NOT NULL,
                    rationale TEXT,
                    fill_provenance TEXT NOT NULL,
                    broker_submission_enabled INTEGER NOT NULL DEFAULT 0,
                    live_authority INTEGER NOT NULL DEFAULT 0,
                    visibility_class TEXT NOT NULL DEFAULT 'VISIBLE',
                    event_count INTEGER NOT NULL DEFAULT 0,
                    last_sequence INTEGER NOT NULL DEFAULT 0,
                    last_event_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS strategy_run_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    market_time_utc TEXT,
                    received_time_utc TEXT NOT NULL,
                    emitted_time_utc TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_event_hash TEXT,
                    event_hash TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence),
                    UNIQUE(event_id),
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS ix_strategy_events_run_type
                    ON strategy_run_events(run_id, event_type, sequence);
                CREATE TABLE IF NOT EXISTS backtest_requests (
                    idempotency_key TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,
                    run_id TEXT NOT NULL UNIQUE,
                    request_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES strategy_runs(run_id)
                );
                """
            )
            c.commit()

    def create_run(
        self,
        *,
        run_id: str,
        strategy_id: str,
        strategy_version: str,
        mode: str,
        source_id: str,
        instrument_id: str,
        parameters: dict[str, Any],
        dataset_hash: str | None = None,
        code_hash: str | None = None,
        config_hash: str | None = None,
        rationale: str | None = None,
        fill_provenance: str = "SIMULATED",
        visibility_class: str = "VISIBLE",
    ) -> dict[str, Any]:
        if mode not in ALLOWED_MODES:
            raise ValueError("unsupported run mode")
        if mode in {"BROKER_DEMO", "LIVE"}:
            raise ValueError("broker-connected modes are not admitted by this registry entry point")
        if fill_provenance != "SIMULATED":
            raise ValueError("research registry only admits simulated fills in current scope")
        if visibility_class not in ALLOWED_VISIBILITY_CLASSES:
            raise ValueError("unsupported visibility class")
        created = _now()
        with self._connect() as c:
            c.execute(
                """
                INSERT INTO strategy_runs(
                    run_id,strategy_id,strategy_version,mode,source_id,instrument_id,
                    status,result,created_at,updated_at,dataset_hash,code_hash,config_hash,
                    parameter_json,rationale,fill_provenance,broker_submission_enabled,
                    live_authority,visibility_class
                ) VALUES (?,?,?,?,?,?, 'QUEUED',NULL,?,?,?,?,?,?,?,?,0,0,?)
                """,
                (
                    run_id, strategy_id, strategy_version, mode, source_id, instrument_id,
                    created, created, dataset_hash, code_hash, config_hash,
                    json.dumps(parameters, sort_keys=True), rationale, fill_provenance,
                    visibility_class,
                ),
            )
            c.commit()
        return self.get_run(run_id)

    def set_status(self, run_id: str, status: str, *, result: str | None = None) -> None:
        updated = _now()
        with self._connect() as c:
            row = c.execute("SELECT status FROM strategy_runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] in TERMINAL_STATUSES and status != row["status"]:
                raise ValueError("terminal run status is immutable")
            c.execute(
                "UPDATE strategy_runs SET status=?, result=COALESCE(?,result), updated_at=? WHERE run_id=?",
                (status, result, updated, run_id),
            )
            c.commit()

    def append_event(
        self,
        run_id: str,
        event_type: str,
        *,
        market_time_utc: str | None,
        payload: dict[str, Any],
        received_time_utc: str | None = None,
        emitted_time_utc: str | None = None,
    ) -> dict[str, Any]:
        if event_type not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"unsupported event type: {event_type}")
        received = received_time_utc or _now()
        emitted = emitted_time_utc or _now()
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT last_sequence,last_event_hash FROM strategy_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            sequence = int(row["last_sequence"]) + 1
            previous_hash = row["last_event_hash"]
            event_id = sha256(f"{run_id}:{sequence}:{event_type}".encode()).hexdigest()
            body = {
                "schema_version": "EQS-STRATEGY-RUN-EVENT-V1",
                "event_id": event_id,
                "sequence": sequence,
                "run_id": run_id,
                "event_type": event_type,
                "market_time_utc": market_time_utc,
                "received_time_utc": received,
                "emitted_time_utc": emitted,
                "payload": payload,
                "previous_event_hash": previous_hash,
            }
            event_hash = sha256(_canonical(body)).hexdigest()
            c.execute(
                """
                INSERT INTO strategy_run_events(
                    run_id,sequence,event_id,event_type,market_time_utc,received_time_utc,
                    emitted_time_utc,payload_json,previous_event_hash,event_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id, sequence, event_id, event_type, market_time_utc, received,
                    emitted, json.dumps(payload, sort_keys=True), previous_hash, event_hash,
                ),
            )
            c.execute(
                """
                UPDATE strategy_runs
                SET last_sequence=?,event_count=event_count+1,last_event_hash=?,updated_at=?,
                    market_start_utc=COALESCE(market_start_utc,?),
                    market_end_utc=COALESCE(?,market_end_utc)
                WHERE run_id=?
                """,
                (sequence, event_hash, emitted, market_time_utc, market_time_utc, run_id),
            )
            c.commit()
        return {**body, "event_hash": event_hash}

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as c:
            row = c.execute("SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        out = dict(row)
        out["parameters"] = json.loads(out.pop("parameter_json"))
        out["broker_submission_enabled"] = bool(out["broker_submission_enabled"])
        out["live_authority"] = bool(out["live_authority"])
        return out

    def list_runs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as c:
            rows = c.execute(
                "SELECT run_id FROM strategy_runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self.get_run(str(row["run_id"])) for row in rows]

    def events(self, run_id: str, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 5000))
        with self._connect() as c:
            rows = c.execute(
                """
                SELECT * FROM strategy_run_events
                WHERE run_id=? AND sequence>?
                ORDER BY sequence ASC LIMIT ?
                """,
                (run_id, int(after), limit),
            ).fetchall()
        return [
            {
                "schema_version": "EQS-STRATEGY-RUN-EVENT-V1",
                "event_id": row["event_id"],
                "sequence": int(row["sequence"]),
                "run_id": row["run_id"],
                "event_type": row["event_type"],
                "market_time_utc": row["market_time_utc"],
                "received_time_utc": row["received_time_utc"],
                "emitted_time_utc": row["emitted_time_utc"],
                "payload": json.loads(row["payload_json"]),
                "previous_event_hash": row["previous_event_hash"],
                "event_hash": row["event_hash"],
            }
            for row in rows
        ]

    def admit_backtest_request(
        self,
        *,
        idempotency_key: str,
        run_id: str,
        request: dict[str, Any],
    ) -> tuple[str, bool]:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("invalid idempotency key")
        request_hash = sha256(_canonical(request)).hexdigest()
        created = _now()
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT request_hash,run_id FROM backtest_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row is not None:
                if row["request_hash"] != request_hash:
                    raise ValueError("idempotency key reused with different request")
                c.commit()
                return str(row["run_id"]), False
            c.execute(
                """
                INSERT INTO backtest_requests(
                    idempotency_key,request_hash,run_id,request_json,created_at
                ) VALUES (?,?,?,?,?)
                """,
                (
                    idempotency_key,
                    request_hash,
                    run_id,
                    json.dumps(request, sort_keys=True),
                    created,
                ),
            )
            c.commit()
        return run_id, True

    def get_backtest_request(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as c:
            row = c.execute(
                "SELECT * FROM backtest_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["request"] = json.loads(out.pop("request_json"))
        return out

    def verify_chain(self, run_id: str) -> bool:
        previous = None
        expected_sequence = 1
        for event in self.events(run_id, after=0, limit=5000):
            if event["sequence"] != expected_sequence or event["previous_event_hash"] != previous:
                return False
            body = dict(event)
            actual = body.pop("event_hash")
            if sha256(_canonical(body)).hexdigest() != actual:
                return False
            previous = actual
            expected_sequence += 1
        run = self.get_run(run_id)
        return run["event_count"] == expected_sequence - 1 and run["last_event_hash"] == previous
