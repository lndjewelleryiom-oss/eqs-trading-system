from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any, Callable


class RuntimeLeaseError(RuntimeError):
    pass


class WatchdogHeadConflictError(RuntimeError):
    pass


class CheckpointCorruptionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    runtime_id: str
    mode: str
    status: str
    owner_id: str | None
    generation: int
    lease_expires_at: datetime | None
    halt_reason: str | None
    created_at: datetime
    updated_at: datetime
    last_heartbeat_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredRuntimeEvent:
    sequence: int
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredCheckpoint:
    checkpoint_id: int
    generation: int
    created_at: datetime
    payload: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredV1IntentBinding:
    client_order_id: str
    execution_intent_id: str
    mode: str
    recorded_at: datetime
    intent: dict[str, Any]
    capability: dict[str, Any]
    interlock: dict[str, Any]
    authority_verification: dict[str, Any]
    intent_sha256: str
    capability_sha256: str
    interlock_sha256: str
    authority_sha256: str


@dataclass(frozen=True, slots=True)
class StoredV1PreDispatchVerification:
    client_order_id: str
    execution_intent_id: str
    recorded_at: datetime
    verification: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredV1ExecutionEvent:
    sequence: int
    event_id: str
    execution_intent_id: str
    client_order_id: str
    runtime_mode: str
    truth_source: str
    authoritative_external_truth: bool
    occurred_at: datetime
    event: dict[str, Any]
    sha256: str




@dataclass(frozen=True, slots=True)
class StoredV1ExternalActionTransition:
    sequence: int
    transition_id: str
    action_id: str
    action_kind: str
    attempt: int
    execution_intent_id: str
    client_order_id: str
    parent_action_id: str | None
    state: str
    occurred_at: datetime
    evidence_class: str
    payload: dict[str, Any]
    sha256: str

@dataclass(frozen=True, slots=True)
class StoredV1ReservationSettlement:
    sequence: int
    settlement_id: str
    reservation_id: str
    execution_id: str
    client_order_id: str
    source_event_id: str
    runtime_mode: str
    truth_source: str
    authoritative_external_truth: bool
    occurred_at: datetime
    settlement: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredV1PrivateReconciliation:
    sequence: int
    reconciliation_id: str
    venue_id: str
    connection_id: str
    completed_at: datetime
    commissioning_ready: bool
    report: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredV1PrivateDrift:
    sequence: int
    drift_id: str
    venue_id: str
    connection_id: str
    previous_reconciliation_id: str | None
    current_reconciliation_id: str
    detected_at: datetime
    status: str
    unresolved: bool
    report: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredV1ReconciliationPolicy:
    policy_id: str
    venue_id: str
    connection_id: str
    cadence_seconds: int
    maximum_age_seconds: int
    anchor_at: datetime
    trading_capable: bool
    policy: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredV1ReconciliationWatchdog:
    sequence: int
    assessment_id: str
    policy_id: str
    venue_id: str
    connection_id: str
    assessed_at: datetime
    last_reconciliation_id: str | None
    next_due_at: datetime
    status: str
    commissioning_hold: bool
    report: dict[str, Any]
    sha256: str


@dataclass(frozen=True, slots=True)
class StoredV1ReconciliationWatchdogHead:
    venue_id: str
    connection_id: str
    assessment_id: str
    sequence: int
    writer_owner_id: str
    writer_generation: int
    updated_at: datetime


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def payload_hash(payload: dict[str, Any]) -> str:
    return sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _parse_time(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("persisted runtime timestamp must be timezone-aware")
    return parsed


class PersistentRuntimeStore:
    """SQLite WAL store for restart-safe paper/shadow orchestration.

    The schema contains no credential fields and no broker-submission configuration.
    """

    def __init__(self, path: str | Path, *, test_fault_injector: Callable[[str], None] | None = None):
        self.path = Path(path)
        self._test_fault_injector = test_fault_injector
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            head_table_existed = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_v1_reconciliation_watchdog_heads'"
            ).fetchone() is not None
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_state (
                    runtime_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL CHECK(mode IN ('PAPER','SHADOW')),
                    status TEXT NOT NULL,
                    owner_id TEXT,
                    generation INTEGER NOT NULL DEFAULT 0,
                    lease_expires_at TEXT,
                    halt_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_heartbeat_at TEXT
                );
                CREATE TABLE IF NOT EXISTS runtime_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    UNIQUE(runtime_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS runtime_checkpoints (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    generation INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_checkpoint_heads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL UNIQUE REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    generation INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    archive_checkpoint_id INTEGER
                );
                CREATE TABLE IF NOT EXISTS execution_v1_bindings (
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    client_order_id TEXT NOT NULL,
                    execution_intent_id TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('PAPER','SHADOW')),
                    recorded_at TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    intent_sha256 TEXT NOT NULL,
                    capability_json TEXT NOT NULL,
                    capability_sha256 TEXT NOT NULL,
                    interlock_json TEXT NOT NULL,
                    interlock_sha256 TEXT NOT NULL,
                    authority_json TEXT NOT NULL,
                    authority_sha256 TEXT NOT NULL,
                    PRIMARY KEY(runtime_id, client_order_id),
                    UNIQUE(runtime_id, execution_intent_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_pre_dispatch (
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    client_order_id TEXT NOT NULL,
                    execution_intent_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    verification_json TEXT NOT NULL,
                    verification_sha256 TEXT NOT NULL,
                    PRIMARY KEY(runtime_id, client_order_id),
                    UNIQUE(runtime_id, execution_intent_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    execution_intent_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    runtime_mode TEXT NOT NULL CHECK(runtime_mode IN ('PAPER','SHADOW')),
                    truth_source TEXT NOT NULL CHECK(truth_source IN ('SIMULATOR','SHADOW_PREVIEW','VENUE')),
                    authoritative_external_truth INTEGER NOT NULL CHECK(authoritative_external_truth IN (0,1)),
                    occurred_at TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    event_sha256 TEXT NOT NULL,
                    UNIQUE(runtime_id, sequence),
                    UNIQUE(runtime_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_reservation_settlements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    settlement_id TEXT NOT NULL,
                    reservation_id TEXT NOT NULL,
                    execution_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    source_event_id TEXT NOT NULL,
                    runtime_mode TEXT NOT NULL CHECK(runtime_mode IN ('PAPER','SHADOW')),
                    truth_source TEXT NOT NULL CHECK(truth_source IN ('SIMULATOR','SHADOW_PREVIEW','VENUE')),
                    authoritative_external_truth INTEGER NOT NULL CHECK(authoritative_external_truth IN (0,1)),
                    occurred_at TEXT NOT NULL,
                    settlement_json TEXT NOT NULL,
                    settlement_sha256 TEXT NOT NULL,
                    UNIQUE(runtime_id, sequence),
                    UNIQUE(runtime_id, settlement_id),
                    UNIQUE(runtime_id, source_event_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_external_action_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    transition_id TEXT NOT NULL,
                    action_id TEXT NOT NULL,
                    action_kind TEXT NOT NULL CHECK(action_kind IN ('SUBMIT','CANCEL')),
                    attempt INTEGER NOT NULL CHECK(attempt >= 1),
                    execution_intent_id TEXT NOT NULL,
                    client_order_id TEXT NOT NULL,
                    parent_action_id TEXT,
                    state TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    evidence_class TEXT NOT NULL CHECK(evidence_class='SYNTHETIC_FIXTURE'),
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    UNIQUE(runtime_id, sequence),
                    UNIQUE(runtime_id, transition_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_private_reconciliations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    reconciliation_id TEXT NOT NULL,
                    venue_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    completed_at TEXT NOT NULL,
                    commissioning_ready INTEGER NOT NULL CHECK(commissioning_ready IN (0,1)),
                    report_json TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    UNIQUE(runtime_id, sequence),
                    UNIQUE(runtime_id, reconciliation_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_private_drift (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    drift_id TEXT NOT NULL,
                    venue_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    previous_reconciliation_id TEXT,
                    current_reconciliation_id TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('BASELINE_ESTABLISHED','STABLE','DRIFT_DETECTED','DRIFT_RESOLVED','BLOCKED_UNKNOWN')),
                    unresolved INTEGER NOT NULL CHECK(unresolved IN (0,1)),
                    report_json TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    UNIQUE(runtime_id, sequence),
                    UNIQUE(runtime_id, drift_id),
                    UNIQUE(runtime_id, current_reconciliation_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_reconciliation_policies (
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    policy_id TEXT NOT NULL,
                    venue_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    cadence_seconds INTEGER NOT NULL CHECK(cadence_seconds > 0),
                    maximum_age_seconds INTEGER NOT NULL CHECK(maximum_age_seconds >= cadence_seconds),
                    anchor_at TEXT NOT NULL,
                    trading_capable INTEGER NOT NULL CHECK(trading_capable IN (0,1)),
                    policy_json TEXT NOT NULL,
                    policy_sha256 TEXT NOT NULL,
                    PRIMARY KEY(runtime_id, policy_id),
                    UNIQUE(runtime_id, venue_id, connection_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_reconciliation_watchdog (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    sequence INTEGER NOT NULL,
                    assessment_id TEXT NOT NULL,
                    policy_id TEXT NOT NULL,
                    venue_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    assessed_at TEXT NOT NULL,
                    last_reconciliation_id TEXT,
                    next_due_at TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('BASELINE_REQUIRED','HEALTHY','DUE','MISSED_CADENCE','MAX_AGE_EXCEEDED')),
                    commissioning_hold INTEGER NOT NULL CHECK(commissioning_hold IN (0,1)),
                    report_json TEXT NOT NULL,
                    report_sha256 TEXT NOT NULL,
                    UNIQUE(runtime_id, sequence),
                    UNIQUE(runtime_id, assessment_id)
                );
                CREATE TABLE IF NOT EXISTS execution_v1_reconciliation_watchdog_heads (
                    runtime_id TEXT NOT NULL REFERENCES runtime_state(runtime_id) ON DELETE CASCADE,
                    venue_id TEXT NOT NULL,
                    connection_id TEXT NOT NULL,
                    assessment_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    writer_owner_id TEXT NOT NULL,
                    writer_generation INTEGER NOT NULL CHECK(writer_generation >= 0),
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(runtime_id, venue_id, connection_id),
                    UNIQUE(runtime_id, assessment_id),
                    FOREIGN KEY(runtime_id, assessment_id)
                        REFERENCES execution_v1_reconciliation_watchdog(runtime_id, assessment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_v1_private_recon_runtime
                    ON execution_v1_private_reconciliations(runtime_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_private_recon_scope
                    ON execution_v1_private_reconciliations(runtime_id, venue_id, connection_id, completed_at);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_private_drift_runtime
                    ON execution_v1_private_drift(runtime_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_private_drift_scope
                    ON execution_v1_private_drift(runtime_id, venue_id, connection_id, detected_at);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_recon_policy_scope
                    ON execution_v1_reconciliation_policies(runtime_id, venue_id, connection_id);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_recon_watchdog_runtime
                    ON execution_v1_reconciliation_watchdog(runtime_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_recon_watchdog_scope
                    ON execution_v1_reconciliation_watchdog(runtime_id, venue_id, connection_id, assessed_at);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_recon_watchdog_head_scope
                    ON execution_v1_reconciliation_watchdog_heads(runtime_id, venue_id, connection_id);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_external_action_runtime
                    ON execution_v1_external_action_journal(runtime_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_external_action_id
                    ON execution_v1_external_action_journal(runtime_id, action_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_bindings_execution
                    ON execution_v1_bindings(runtime_id, execution_intent_id);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_pre_dispatch_execution
                    ON execution_v1_pre_dispatch(runtime_id, execution_intent_id);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_events_runtime
                    ON execution_v1_events(runtime_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_settlements_runtime
                    ON execution_v1_reservation_settlements(runtime_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_execution_v1_settlements_execution
                    ON execution_v1_reservation_settlements(runtime_id, execution_id);
                CREATE INDEX IF NOT EXISTS idx_runtime_events_runtime ON runtime_events(runtime_id, sequence);
                CREATE INDEX IF NOT EXISTS idx_runtime_checkpoints_runtime ON runtime_checkpoints(runtime_id, id DESC);
                """
            )
            checkpoint_head_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(runtime_checkpoint_heads)").fetchall()
            }
            if "archive_checkpoint_id" not in checkpoint_head_columns:
                connection.execute(
                    "ALTER TABLE runtime_checkpoint_heads ADD COLUMN archive_checkpoint_id INTEGER"
                )
            if not head_table_existed:
                connection.execute(
                    """INSERT INTO execution_v1_reconciliation_watchdog_heads(
                    runtime_id, venue_id, connection_id, assessment_id, sequence, writer_owner_id, writer_generation, updated_at
                    )
                    SELECT w.runtime_id, w.venue_id, w.connection_id, w.assessment_id, w.sequence,
                           'MIGRATED', 0, w.assessed_at
                    FROM execution_v1_reconciliation_watchdog AS w
                    JOIN (
                        SELECT runtime_id, venue_id, connection_id, MAX(sequence) AS max_sequence
                        FROM execution_v1_reconciliation_watchdog
                        GROUP BY runtime_id, venue_id, connection_id
                    ) AS latest
                      ON latest.runtime_id=w.runtime_id
                     AND latest.venue_id=w.venue_id
                     AND latest.connection_id=w.connection_id
                     AND latest.max_sequence=w.sequence
                    """
                )

    def claim(self, runtime_id: str, mode: str, owner_id: str, *, now: datetime, lease_seconds: int) -> RuntimeRecord:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        expires = now + timedelta(seconds=lease_seconds)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO runtime_state
                    (runtime_id, mode, status, owner_id, generation, lease_expires_at, halt_reason, created_at, updated_at, last_heartbeat_at)
                    VALUES (?, ?, 'STARTING', ?, 1, ?, NULL, ?, ?, ?)""",
                    (runtime_id, mode, owner_id, expires.isoformat(), now.isoformat(), now.isoformat(), now.isoformat()),
                )
            else:
                if row["mode"] != mode:
                    raise RuntimeLeaseError(f"runtime mode mismatch: stored={row['mode']} requested={mode}")
                current_expiry = _parse_time(row["lease_expires_at"])
                if row["owner_id"] not in (None, owner_id) and current_expiry is not None and current_expiry > now:
                    raise RuntimeLeaseError("runtime lease is held by another owner")
                connection.execute(
                    """UPDATE runtime_state
                    SET owner_id=?, generation=generation+1, lease_expires_at=?, updated_at=?, last_heartbeat_at=?
                    WHERE runtime_id=?""",
                    (owner_id, expires.isoformat(), now.isoformat(), now.isoformat(), runtime_id),
                )
            connection.commit()
        record = self.get_runtime(runtime_id)
        assert record is not None
        return record

    def renew(self, runtime_id: str, owner_id: str, *, now: datetime, lease_seconds: int) -> RuntimeRecord:
        expires = now + timedelta(seconds=lease_seconds)
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE runtime_state SET lease_expires_at=?, updated_at=?, last_heartbeat_at=?
                WHERE runtime_id=? AND owner_id=?""",
                (expires.isoformat(), now.isoformat(), now.isoformat(), runtime_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeLeaseError("runtime lease is not owned by this process")
        record = self.get_runtime(runtime_id)
        assert record is not None
        return record

    def release(self, runtime_id: str, owner_id: str, *, now: datetime) -> None:
        with self._connection() as connection:
            connection.execute(
                """UPDATE runtime_state SET owner_id=NULL, lease_expires_at=NULL, updated_at=?
                WHERE runtime_id=? AND owner_id=?""",
                (now.isoformat(), runtime_id, owner_id),
            )

    def set_status(self, runtime_id: str, owner_id: str, *, status: str, halt_reason: str | None, now: datetime) -> None:
        with self._connection() as connection:
            cursor = connection.execute(
                """UPDATE runtime_state SET status=?, halt_reason=?, updated_at=?
                WHERE runtime_id=? AND owner_id=?""",
                (status, halt_reason, now.isoformat(), runtime_id, owner_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeLeaseError("cannot update runtime state without its lease")

    def append_event(self, runtime_id: str, owner_id: str, *, event_type: str, occurred_at: datetime, payload: dict[str, Any]) -> int:
        serialized = canonical_json(payload)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot append event without runtime lease")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM runtime_events WHERE runtime_id=?", (runtime_id,)
            ).fetchone()["next_sequence"]
            connection.execute(
                """INSERT INTO runtime_events(runtime_id, sequence, event_type, occurred_at, payload_json, payload_sha256)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (runtime_id, sequence, event_type, occurred_at.isoformat(), serialized, digest),
            )
            connection.commit()
        return int(sequence)

    def bind_v1_intent(
        self,
        runtime_id: str,
        owner_id: str,
        *,
        client_order_id: str,
        execution_intent_id: str,
        mode: str,
        recorded_at: datetime,
        intent: dict[str, Any],
        capability: dict[str, Any],
        interlock: dict[str, Any],
        authority_verification: dict[str, Any],
    ) -> StoredV1IntentBinding:
        payloads = {
            "intent": intent,
            "capability": capability,
            "interlock": interlock,
            "authority": authority_verification,
        }
        serialized = {name: canonical_json(value) for name, value in payloads.items()}
        digests = {name: sha256(value.encode("utf-8")).hexdigest() for name, value in serialized.items()}
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot bind V1 intent without runtime lease")
            existing = connection.execute(
                "SELECT * FROM execution_v1_bindings WHERE runtime_id=? AND client_order_id=?",
                (runtime_id, client_order_id),
            ).fetchone()
            if existing is not None:
                expected = (
                    execution_intent_id, mode, digests["intent"], digests["capability"],
                    digests["interlock"], digests["authority"],
                )
                actual = (
                    existing["execution_intent_id"], existing["mode"], existing["intent_sha256"],
                    existing["capability_sha256"], existing["interlock_sha256"], existing["authority_sha256"],
                )
                if actual != expected:
                    raise ValueError("conflicting V1 intent binding for client_order_id")
            else:
                connection.execute(
                    """INSERT INTO execution_v1_bindings(
                    runtime_id, client_order_id, execution_intent_id, mode, recorded_at,
                    intent_json, intent_sha256, capability_json, capability_sha256,
                    interlock_json, interlock_sha256, authority_json, authority_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (runtime_id, client_order_id, execution_intent_id, mode, recorded_at.isoformat(),
                     serialized["intent"], digests["intent"], serialized["capability"], digests["capability"],
                     serialized["interlock"], digests["interlock"], serialized["authority"], digests["authority"]),
                )
                connection.commit()
        result = self.get_v1_intent_binding(runtime_id, client_order_id)
        assert result is not None
        return result

    def get_v1_intent_binding(self, runtime_id: str, client_order_id: str) -> StoredV1IntentBinding | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v1_bindings WHERE runtime_id=? AND client_order_id=?",
                (runtime_id, client_order_id),
            ).fetchone()
        if row is None:
            return None
        payload_columns = (
            ("intent", "intent_json", "intent_sha256"),
            ("capability", "capability_json", "capability_sha256"),
            ("interlock", "interlock_json", "interlock_sha256"),
            ("authority", "authority_json", "authority_sha256"),
        )
        decoded: dict[str, dict[str, Any]] = {}
        for name, json_column, hash_column in payload_columns:
            raw = str(row[json_column])
            if sha256(raw.encode("utf-8")).hexdigest() != row[hash_column]:
                raise CheckpointCorruptionError(f"V1 {name} binding hash verification failed")
            decoded[name] = json.loads(raw)
        return StoredV1IntentBinding(
            client_order_id=str(row["client_order_id"]),
            execution_intent_id=str(row["execution_intent_id"]),
            mode=str(row["mode"]),
            recorded_at=_parse_time(row["recorded_at"]),
            intent=decoded["intent"], capability=decoded["capability"], interlock=decoded["interlock"],
            authority_verification=decoded["authority"],
            intent_sha256=str(row["intent_sha256"]), capability_sha256=str(row["capability_sha256"]),
            interlock_sha256=str(row["interlock_sha256"]), authority_sha256=str(row["authority_sha256"]),
        )

    def list_v1_intent_bindings(self, runtime_id: str) -> tuple[StoredV1IntentBinding, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT client_order_id FROM execution_v1_bindings WHERE runtime_id=? ORDER BY recorded_at, client_order_id",
                (runtime_id,),
            ).fetchall()
        bindings: list[StoredV1IntentBinding] = []
        for row in rows:
            binding = self.get_v1_intent_binding(runtime_id, str(row["client_order_id"]))
            assert binding is not None
            bindings.append(binding)
        return tuple(bindings)

    def bind_v1_pre_dispatch(
        self,
        runtime_id: str,
        owner_id: str,
        *,
        client_order_id: str,
        execution_intent_id: str,
        recorded_at: datetime,
        verification: dict[str, Any],
    ) -> StoredV1PreDispatchVerification:
        serialized = canonical_json(verification)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot bind V1 pre-dispatch verification without runtime lease")
            binding = connection.execute(
                "SELECT execution_intent_id FROM execution_v1_bindings WHERE runtime_id=? AND client_order_id=?",
                (runtime_id, client_order_id),
            ).fetchone()
            if binding is None or str(binding["execution_intent_id"]) != execution_intent_id:
                raise ValueError("V1 pre-dispatch verification requires matching intent binding")
            existing = connection.execute(
                "SELECT * FROM execution_v1_pre_dispatch WHERE runtime_id=? AND client_order_id=?",
                (runtime_id, client_order_id),
            ).fetchone()
            if existing is not None:
                expected = (execution_intent_id, digest)
                actual = (str(existing["execution_intent_id"]), str(existing["verification_sha256"]))
                if actual != expected:
                    raise ValueError("conflicting V1 pre-dispatch verification for client_order_id")
            else:
                connection.execute(
                    """INSERT INTO execution_v1_pre_dispatch(
                    runtime_id, client_order_id, execution_intent_id, recorded_at, verification_json, verification_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?)""",
                    (runtime_id, client_order_id, execution_intent_id, recorded_at.isoformat(), serialized, digest),
                )
                connection.commit()
        result = self.get_v1_pre_dispatch(runtime_id, client_order_id)
        assert result is not None
        return result

    def get_v1_pre_dispatch(self, runtime_id: str, client_order_id: str) -> StoredV1PreDispatchVerification | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v1_pre_dispatch WHERE runtime_id=? AND client_order_id=?",
                (runtime_id, client_order_id),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["verification_json"])
        digest = sha256(raw.encode("utf-8")).hexdigest()
        if digest != row["verification_sha256"]:
            raise CheckpointCorruptionError("V1 pre-dispatch verification hash verification failed")
        return StoredV1PreDispatchVerification(
            client_order_id=str(row["client_order_id"]),
            execution_intent_id=str(row["execution_intent_id"]),
            recorded_at=_parse_time(row["recorded_at"]),
            verification=json.loads(raw),
            sha256=str(row["verification_sha256"]),
        )

    def list_v1_pre_dispatch(self, runtime_id: str) -> tuple[StoredV1PreDispatchVerification, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT client_order_id FROM execution_v1_pre_dispatch WHERE runtime_id=? ORDER BY recorded_at, client_order_id",
                (runtime_id,),
            ).fetchall()
        values: list[StoredV1PreDispatchVerification] = []
        for row in rows:
            item = self.get_v1_pre_dispatch(runtime_id, str(row["client_order_id"]))
            assert item is not None
            values.append(item)
        return tuple(values)

    def verify_v1_pre_dispatch_journal(self, runtime_id: str) -> int:
        values = self.list_v1_pre_dispatch(runtime_id)
        bindings = {item.client_order_id: item for item in self.list_v1_intent_bindings(runtime_id)}
        by_client = {item.client_order_id: item for item in values}
        for client_order_id, binding in bindings.items():
            if client_order_id not in by_client:
                raise CheckpointCorruptionError("V1 intent binding is missing required pre-dispatch verification")
            item = by_client[client_order_id]
            if binding.execution_intent_id != item.execution_intent_id:
                raise CheckpointCorruptionError("V1 pre-dispatch verification execution identity mismatch")
            payload = item.verification
            if str(payload.get("execution_intent_id")) != item.execution_intent_id:
                raise CheckpointCorruptionError("V1 pre-dispatch verification payload execution identity mismatch")
            if str(payload.get("connection_id")) != str(binding.capability.get("connection_id")):
                raise CheckpointCorruptionError("V1 pre-dispatch verification connection identity mismatch")
            if str(payload.get("venue_id")) != str(binding.intent.get("route", {}).get("venue_id")):
                raise CheckpointCorruptionError("V1 pre-dispatch verification venue identity mismatch")
            if str(payload.get("instrument_id")) != str(binding.intent.get("instrument", {}).get("instrument_id")):
                raise CheckpointCorruptionError("V1 pre-dispatch verification instrument identity mismatch")
            checks = (
                ("constraint_snapshot_hash", "constraint_snapshot"),
                ("venue_state_hash", "venue_state_snapshot"),
                ("session_state_hash", "session_state_snapshot"),
            )
            for hash_key, payload_key in checks:
                value = str(payload.get(hash_key, ""))
                if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value.lower()):
                    raise CheckpointCorruptionError(f"V1 pre-dispatch verification invalid {hash_key}")
                snapshot = payload.get(payload_key)
                if not isinstance(snapshot, dict) or payload_hash(snapshot) != value:
                    raise CheckpointCorruptionError(f"V1 pre-dispatch verification {payload_key} hash mismatch")
            if str(payload.get("constraint_snapshot_id")) != str(payload["constraint_snapshot"].get("snapshot_id")):
                raise CheckpointCorruptionError("V1 pre-dispatch constraint snapshot identity mismatch")
        return len(values)

    def append_v1_execution_event(
        self,
        runtime_id: str,
        owner_id: str,
        *,
        event_id: str,
        execution_intent_id: str,
        client_order_id: str,
        runtime_mode: str,
        truth_source: str,
        authoritative_external_truth: bool,
        occurred_at: datetime,
        event: dict[str, Any],
    ) -> int:
        serialized = canonical_json(event)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot append V1 execution event without runtime lease")
            existing = connection.execute(
                "SELECT sequence, event_sha256, execution_intent_id, client_order_id, runtime_mode, truth_source, authoritative_external_truth "
                "FROM execution_v1_events WHERE runtime_id=? AND event_id=?",
                (runtime_id, event_id),
            ).fetchone()
            if existing is not None:
                expected = (digest, execution_intent_id, client_order_id, runtime_mode, truth_source, int(authoritative_external_truth))
                actual = (str(existing["event_sha256"]), str(existing["execution_intent_id"]), str(existing["client_order_id"]),
                          str(existing["runtime_mode"]), str(existing["truth_source"]), int(existing["authoritative_external_truth"]))
                if actual != expected:
                    raise ValueError("conflicting V1 execution event for event_id")
                return int(existing["sequence"])
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM execution_v1_events WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()["next_sequence"]
            connection.execute(
                """INSERT INTO execution_v1_events(
                runtime_id, sequence, event_id, execution_intent_id, client_order_id, runtime_mode, truth_source,
                authoritative_external_truth, occurred_at, event_json, event_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (runtime_id, sequence, event_id, execution_intent_id, client_order_id, runtime_mode, truth_source,
                 int(authoritative_external_truth), occurred_at.isoformat(), serialized, digest),
            )
            connection.commit()
        return int(sequence)

    def append_v1_execution_event_with_settlement(
        self,
        runtime_id: str,
        owner_id: str,
        *,
        event_id: str,
        execution_intent_id: str,
        client_order_id: str,
        runtime_mode: str,
        truth_source: str,
        authoritative_external_truth: bool,
        occurred_at: datetime,
        event: dict[str, Any],
        settlement_id: str,
        reservation_id: str,
        settlement: dict[str, Any],
    ) -> tuple[int, int]:
        """Atomically append a validated, idempotent V1 event and settlement."""
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = self._append_v1_settlement_in_transaction(connection, runtime_id, owner_id, event_id=event_id, execution_intent_id=execution_intent_id, client_order_id=client_order_id, runtime_mode=runtime_mode, truth_source=truth_source, authoritative_external_truth=authoritative_external_truth, occurred_at=occurred_at, event=event, settlement_id=settlement_id, reservation_id=reservation_id, settlement=settlement)
            connection.commit()
        return result

    def _append_v1_settlement_in_transaction(
        self,
        connection: sqlite3.Connection,
        runtime_id: str,
        owner_id: str,
        *,
        event_id: str,
        execution_intent_id: str,
        client_order_id: str,
        runtime_mode: str,
        truth_source: str,
        authoritative_external_truth: bool,
        occurred_at: datetime,
        event: dict[str, Any],
        settlement_id: str,
        reservation_id: str,
        settlement: dict[str, Any],
    ) -> tuple[int, int]:
        """Atomically append a V1 execution event and its reservation settlement.

        Both records are independently idempotent. A retry with identical canonical
        payloads returns the original sequences; any conflicting reuse of either ID or
        source event fails closed.
        """
        expected_settlement_event = {
            "PARTIAL_FILL": "PARTIAL_FILL",
            "FILL": "FILLED",
            "CANCELLED": "CANCELLED",
            "REJECTED": "REJECTED",
        }.get(str(event.get("event_type")))
        if str(event.get("event_id")) != event_id or str(event.get("execution_id")) != execution_intent_id:
            raise ValueError("V1 execution event envelope does not match canonical payload")
        if str(event.get("client_order_id")) != client_order_id:
            raise ValueError("V1 execution event client order does not match canonical payload")
        if expected_settlement_event is None or str(settlement.get("event")) != expected_settlement_event:
            raise ValueError("V1 reservation settlement type does not match source execution event")
        if str(settlement.get("settlement_id")) != settlement_id:
            raise ValueError("V1 reservation settlement ID does not match canonical payload")
        if str(settlement.get("reservation_id")) != reservation_id or str(settlement.get("execution_id")) != execution_intent_id:
            raise ValueError("V1 reservation settlement identity does not match canonical payload")
        if settlement.get("execution_truth_ref") != event_id:
            raise ValueError("V1 reservation settlement must reference its source execution event")

        event_serialized = canonical_json(event)
        event_digest = sha256(event_serialized.encode("utf-8")).hexdigest()
        settlement_serialized = canonical_json(settlement)
        settlement_digest = sha256(settlement_serialized.encode("utf-8")).hexdigest()
        external_flag = int(authoritative_external_truth)
        lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
        if lease is None or lease["owner_id"] != owner_id:
            raise RuntimeLeaseError("cannot append V1 settlement without runtime lease")

        existing_event = connection.execute(
            "SELECT sequence, event_sha256, execution_intent_id, client_order_id, runtime_mode, truth_source, "
            "authoritative_external_truth FROM execution_v1_events WHERE runtime_id=? AND event_id=?",
            (runtime_id, event_id),
        ).fetchone()
        if existing_event is None:
            event_sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM execution_v1_events WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()["next_sequence"])
            connection.execute(
                """INSERT INTO execution_v1_events(
                runtime_id, sequence, event_id, execution_intent_id, client_order_id, runtime_mode, truth_source,
                authoritative_external_truth, occurred_at, event_json, event_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (runtime_id, event_sequence, event_id, execution_intent_id, client_order_id, runtime_mode, truth_source,
                 external_flag, occurred_at.isoformat(), event_serialized, event_digest),
            )
        else:
            expected_event = (event_digest, execution_intent_id, client_order_id, runtime_mode, truth_source, external_flag)
            actual_event = (
                str(existing_event["event_sha256"]), str(existing_event["execution_intent_id"]),
                str(existing_event["client_order_id"]), str(existing_event["runtime_mode"]),
                str(existing_event["truth_source"]), int(existing_event["authoritative_external_truth"]),
            )
            if actual_event != expected_event:
                raise ValueError("conflicting V1 execution event for event_id")
            event_sequence = int(existing_event["sequence"])

        conflicting_source = connection.execute(
            "SELECT settlement_id FROM execution_v1_reservation_settlements WHERE runtime_id=? AND source_event_id=?",
            (runtime_id, event_id),
        ).fetchone()
        if conflicting_source is not None and str(conflicting_source["settlement_id"]) != settlement_id:
            raise ValueError("V1 execution event is already linked to a different reservation settlement")

        existing_settlement = connection.execute(
            "SELECT sequence, settlement_sha256, reservation_id, execution_id, client_order_id, source_event_id, "
            "runtime_mode, truth_source, authoritative_external_truth "
            "FROM execution_v1_reservation_settlements WHERE runtime_id=? AND settlement_id=?",
            (runtime_id, settlement_id),
        ).fetchone()
        if existing_settlement is None:
            settlement_sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence "
                "FROM execution_v1_reservation_settlements WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()["next_sequence"])
            connection.execute(
                """INSERT INTO execution_v1_reservation_settlements(
                runtime_id, sequence, settlement_id, reservation_id, execution_id, client_order_id, source_event_id,
                runtime_mode, truth_source, authoritative_external_truth, occurred_at, settlement_json, settlement_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (runtime_id, settlement_sequence, settlement_id, reservation_id, execution_intent_id, client_order_id,
                 event_id, runtime_mode, truth_source, external_flag, occurred_at.isoformat(),
                 settlement_serialized, settlement_digest),
            )
        else:
            expected_settlement = (
                settlement_digest, reservation_id, execution_intent_id, client_order_id, event_id,
                runtime_mode, truth_source, external_flag,
            )
            actual_settlement = (
                str(existing_settlement["settlement_sha256"]), str(existing_settlement["reservation_id"]),
                str(existing_settlement["execution_id"]), str(existing_settlement["client_order_id"]),
                str(existing_settlement["source_event_id"]), str(existing_settlement["runtime_mode"]),
                str(existing_settlement["truth_source"]), int(existing_settlement["authoritative_external_truth"]),
            )
            if actual_settlement != expected_settlement:
                raise ValueError("conflicting V1 reservation settlement for settlement_id")
            settlement_sequence = int(existing_settlement["sequence"])
        return event_sequence, settlement_sequence

    def load_v1_reservation_settlements(self, runtime_id: str) -> tuple[StoredV1ReservationSettlement, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_v1_reservation_settlements WHERE runtime_id=? ORDER BY sequence",
                (runtime_id,),
            ).fetchall()
        settlements: list[StoredV1ReservationSettlement] = []
        previous = 0
        for row in rows:
            sequence = int(row["sequence"])
            if sequence != previous + 1:
                raise CheckpointCorruptionError("V1 reservation settlement sequence is not contiguous")
            previous = sequence
            raw = str(row["settlement_json"])
            digest = sha256(raw.encode("utf-8")).hexdigest()
            if digest != row["settlement_sha256"]:
                raise CheckpointCorruptionError("V1 reservation settlement hash verification failed")
            settlements.append(StoredV1ReservationSettlement(
                sequence=sequence, settlement_id=str(row["settlement_id"]), reservation_id=str(row["reservation_id"]),
                execution_id=str(row["execution_id"]), client_order_id=str(row["client_order_id"]),
                source_event_id=str(row["source_event_id"]), runtime_mode=str(row["runtime_mode"]),
                truth_source=str(row["truth_source"]),
                authoritative_external_truth=bool(row["authoritative_external_truth"]),
                occurred_at=_parse_time(row["occurred_at"]), settlement=json.loads(raw),
                sha256=str(row["settlement_sha256"]),
            ))
        return tuple(settlements)

    def list_v1_reservation_settlements_for_execution(
        self, runtime_id: str, execution_id: str
    ) -> tuple[StoredV1ReservationSettlement, ...]:
        return tuple(
            item for item in self.load_v1_reservation_settlements(runtime_id) if item.execution_id == execution_id
        )

    def load_v1_execution_events(self, runtime_id: str) -> tuple[StoredV1ExecutionEvent, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM execution_v1_events WHERE runtime_id=? ORDER BY sequence", (runtime_id,)
            ).fetchall()
        events: list[StoredV1ExecutionEvent] = []
        previous = 0
        for row in rows:
            sequence = int(row["sequence"])
            if sequence != previous + 1:
                raise CheckpointCorruptionError("V1 execution event sequence is not contiguous")
            previous = sequence
            raw = str(row["event_json"])
            digest = sha256(raw.encode("utf-8")).hexdigest()
            if digest != row["event_sha256"]:
                raise CheckpointCorruptionError("V1 execution event hash verification failed")
            events.append(StoredV1ExecutionEvent(
                sequence=sequence, event_id=str(row["event_id"]), execution_intent_id=str(row["execution_intent_id"]),
                client_order_id=str(row["client_order_id"]), runtime_mode=str(row["runtime_mode"]),
                truth_source=str(row["truth_source"]), authoritative_external_truth=bool(row["authoritative_external_truth"]),
                occurred_at=_parse_time(row["occurred_at"]), event=json.loads(raw), sha256=str(row["event_sha256"]),
            ))
        return tuple(events)

    def verify_v1_execution_journal(self, runtime_id: str) -> int:
        with self._connection() as connection:
            binding_rows = connection.execute(
                "SELECT intent_json, intent_sha256, capability_json, capability_sha256, interlock_json, interlock_sha256, "
                "authority_json, authority_sha256 FROM execution_v1_bindings WHERE runtime_id=?", (runtime_id,)
            ).fetchall()
        for row in binding_rows:
            for json_col, hash_col in (("intent_json","intent_sha256"),("capability_json","capability_sha256"),
                                       ("interlock_json","interlock_sha256"),("authority_json","authority_sha256")):
                if sha256(str(row[json_col]).encode("utf-8")).hexdigest() != row[hash_col]:
                    raise CheckpointCorruptionError("V1 authority binding hash verification failed")

        events = self.load_v1_execution_events(runtime_id)
        settlements = self.load_v1_reservation_settlements(runtime_id)
        event_by_id = {item.event_id: item for item in events}
        bindings = {item.client_order_id: item for item in self.list_v1_intent_bindings(runtime_id)}
        event_to_settlement = {
            "PARTIAL_FILL": "PARTIAL_FILL",
            "FILL": "FILLED",
            "CANCELLED": "CANCELLED",
            "REJECTED": "REJECTED",
        }
        for settlement in settlements:
            source = event_by_id.get(settlement.source_event_id)
            if source is None:
                raise CheckpointCorruptionError("V1 reservation settlement source event is missing")
            binding = bindings.get(settlement.client_order_id)
            if binding is None:
                raise CheckpointCorruptionError("V1 reservation settlement authority binding is missing")
            expected_reservation = str(binding.intent["reservation"]["reservation_id"])
            if settlement.reservation_id != expected_reservation or settlement.execution_id != binding.execution_intent_id:
                raise CheckpointCorruptionError("V1 reservation settlement binding identity mismatch")
            if source.execution_intent_id != settlement.execution_id or source.client_order_id != settlement.client_order_id:
                raise CheckpointCorruptionError("V1 reservation settlement source identity mismatch")
            if (source.runtime_mode, source.truth_source, source.authoritative_external_truth) != (
                settlement.runtime_mode, settlement.truth_source, settlement.authoritative_external_truth
            ):
                raise CheckpointCorruptionError("V1 reservation settlement truth envelope mismatch")
            expected_event = event_to_settlement.get(str(source.event.get("event_type")))
            if expected_event is None or str(settlement.settlement.get("event")) != expected_event:
                raise CheckpointCorruptionError("V1 reservation settlement event type mismatch")
            if settlement.settlement.get("execution_truth_ref") != settlement.source_event_id:
                raise CheckpointCorruptionError("V1 reservation settlement source reference mismatch")
        return len(events)

    def append_v1_external_action_transition(
        self,
        runtime_id: str,
        owner_id: str,
        *,
        transition_id: str,
        action_id: str,
        action_kind: str,
        attempt: int,
        execution_intent_id: str,
        client_order_id: str,
        parent_action_id: str | None,
        state: str,
        occurred_at: datetime,
        evidence_class: str,
        payload: dict[str, Any],
    ) -> StoredV1ExternalActionTransition:
        """Append one synthetic external-action state transition, fail closed on illegal reuse."""
        if evidence_class != "SYNTHETIC_FIXTURE":
            raise ValueError("external action protocol is synthetic-fixture only")
        if attempt < 1:
            raise ValueError("external action attempt must be >= 1")
        serialized = canonical_json(payload)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        allowed = {
            None: {"PREPARED"},
            "PREPARED": {"DISPATCHING"},
            "DISPATCHING": {"UNKNOWN_SUBMISSION_OUTCOME", "UNKNOWN_CANCEL_OUTCOME", "RESOLVED_ACKNOWLEDGED", "RESOLVED_REJECTED", "RESOLVED_CANCELLED"},
            "UNKNOWN_SUBMISSION_OUTCOME": {"RECONCILIATION_REQUIRED"},
            "UNKNOWN_CANCEL_OUTCOME": {"RECONCILIATION_REQUIRED"},
            "RECONCILIATION_REQUIRED": {"RECONCILIATION_REQUIRED", "RESOLVED_ACKNOWLEDGED", "RESOLVED_REJECTED", "RESOLVED_ABSENT", "RESOLVED_CANCELLED"},
            "RESOLVED_ACKNOWLEDGED": set(),
            "RESOLVED_REJECTED": set(),
            "RESOLVED_ABSENT": set(),
            "RESOLVED_CANCELLED": set(),
        }
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot append V1 external action transition without runtime lease")
            existing = connection.execute(
                "SELECT * FROM execution_v1_external_action_journal WHERE runtime_id=? AND transition_id=?",
                (runtime_id, transition_id),
            ).fetchone()
            if existing is not None:
                expected = (
                    action_id, action_kind, attempt, execution_intent_id, client_order_id,
                    parent_action_id, state, evidence_class, digest,
                )
                actual = (
                    str(existing["action_id"]), str(existing["action_kind"]), int(existing["attempt"]),
                    str(existing["execution_intent_id"]), str(existing["client_order_id"]),
                    existing["parent_action_id"], str(existing["state"]), str(existing["evidence_class"]),
                    str(existing["payload_sha256"]),
                )
                if actual != expected:
                    raise ValueError("conflicting V1 external action transition for transition_id")
                sequence = int(existing["sequence"])
            else:
                latest = connection.execute(
                    "SELECT * FROM execution_v1_external_action_journal WHERE runtime_id=? AND action_id=? ORDER BY sequence DESC LIMIT 1",
                    (runtime_id, action_id),
                ).fetchone()
                previous_state = None if latest is None else str(latest["state"])
                if state not in allowed.get(previous_state, set()):
                    raise ValueError(f"illegal V1 external action transition: {previous_state}->{state}")
                if latest is not None:
                    identity = (
                        str(latest["action_kind"]), int(latest["attempt"]), str(latest["execution_intent_id"]),
                        str(latest["client_order_id"]), latest["parent_action_id"],
                    )
                    requested = (action_kind, attempt, execution_intent_id, client_order_id, parent_action_id)
                    if identity != requested:
                        raise ValueError("V1 external action identity changed across transitions")
                else:
                    binding = connection.execute(
                        "SELECT execution_intent_id FROM execution_v1_bindings WHERE runtime_id=? AND client_order_id=?",
                        (runtime_id, client_order_id),
                    ).fetchone()
                    if binding is None or str(binding["execution_intent_id"]) != execution_intent_id:
                        raise ValueError("V1 external action is not bound to a canonical intent")
                    if parent_action_id is not None:
                        parent = connection.execute(
                            "SELECT attempt, state FROM execution_v1_external_action_journal WHERE runtime_id=? AND action_id=? ORDER BY sequence DESC LIMIT 1",
                            (runtime_id, parent_action_id),
                        ).fetchone()
                        if parent is None or str(parent["state"]) != "RESOLVED_ABSENT" or int(parent["attempt"]) + 1 != attempt:
                            raise ValueError("V1 external action retry parent is not reconciled absent")
                sequence = int(connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM execution_v1_external_action_journal WHERE runtime_id=?",
                    (runtime_id,),
                ).fetchone()["next_sequence"])
                connection.execute(
                    """INSERT INTO execution_v1_external_action_journal(
                    runtime_id, sequence, transition_id, action_id, action_kind, attempt, execution_intent_id,
                    client_order_id, parent_action_id, state, occurred_at, evidence_class, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (runtime_id, sequence, transition_id, action_id, action_kind, attempt, execution_intent_id,
                     client_order_id, parent_action_id, state, occurred_at.isoformat(), evidence_class, serialized, digest),
                )
                connection.commit()
        result = self.get_v1_external_action_transition(runtime_id, transition_id)
        assert result is not None
        return result

    def get_v1_external_action_transition(
        self, runtime_id: str, transition_id: str
    ) -> StoredV1ExternalActionTransition | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v1_external_action_journal WHERE runtime_id=? AND transition_id=?",
                (runtime_id, transition_id),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["payload_json"])
        digest = sha256(raw.encode("utf-8")).hexdigest()
        if digest != row["payload_sha256"]:
            raise CheckpointCorruptionError("V1 external action transition hash verification failed")
        return StoredV1ExternalActionTransition(
            sequence=int(row["sequence"]), transition_id=str(row["transition_id"]), action_id=str(row["action_id"]),
            action_kind=str(row["action_kind"]), attempt=int(row["attempt"]),
            execution_intent_id=str(row["execution_intent_id"]), client_order_id=str(row["client_order_id"]),
            parent_action_id=row["parent_action_id"], state=str(row["state"]), occurred_at=_parse_time(row["occurred_at"]),
            evidence_class=str(row["evidence_class"]), payload=json.loads(raw), sha256=str(row["payload_sha256"]),
        )

    def load_v1_external_action_transitions(self, runtime_id: str) -> tuple[StoredV1ExternalActionTransition, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT transition_id FROM execution_v1_external_action_journal WHERE runtime_id=? ORDER BY sequence",
                (runtime_id,),
            ).fetchall()
        result: list[StoredV1ExternalActionTransition] = []
        previous = 0
        for row in rows:
            item = self.get_v1_external_action_transition(runtime_id, str(row["transition_id"]))
            assert item is not None
            if item.sequence != previous + 1:
                raise CheckpointCorruptionError("V1 external action journal sequence is not contiguous")
            previous = item.sequence
            result.append(item)
        return tuple(result)

    def get_v1_external_action_latest(self, runtime_id: str, action_id: str) -> StoredV1ExternalActionTransition | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT transition_id FROM execution_v1_external_action_journal WHERE runtime_id=? AND action_id=? ORDER BY sequence DESC LIMIT 1",
                (runtime_id, action_id),
            ).fetchone()
        return None if row is None else self.get_v1_external_action_transition(runtime_id, str(row["transition_id"]))

    def list_v1_external_action_latest(self, runtime_id: str) -> tuple[StoredV1ExternalActionTransition, ...]:
        transitions = self.load_v1_external_action_transitions(runtime_id)
        latest: dict[str, StoredV1ExternalActionTransition] = {}
        for item in transitions:
            latest[item.action_id] = item
        return tuple(sorted(latest.values(), key=lambda item: (item.sequence, item.action_id)))

    def verify_v1_external_action_journal(self, runtime_id: str) -> int:
        transitions = self.load_v1_external_action_transitions(runtime_id)
        allowed = {
            None: {"PREPARED"},
            "PREPARED": {"DISPATCHING"},
            "DISPATCHING": {"UNKNOWN_SUBMISSION_OUTCOME", "UNKNOWN_CANCEL_OUTCOME", "RESOLVED_ACKNOWLEDGED", "RESOLVED_REJECTED", "RESOLVED_CANCELLED"},
            "UNKNOWN_SUBMISSION_OUTCOME": {"RECONCILIATION_REQUIRED"},
            "UNKNOWN_CANCEL_OUTCOME": {"RECONCILIATION_REQUIRED"},
            "RECONCILIATION_REQUIRED": {"RECONCILIATION_REQUIRED", "RESOLVED_ACKNOWLEDGED", "RESOLVED_REJECTED", "RESOLVED_ABSENT", "RESOLVED_CANCELLED"},
            "RESOLVED_ACKNOWLEDGED": set(), "RESOLVED_REJECTED": set(), "RESOLVED_ABSENT": set(), "RESOLVED_CANCELLED": set(),
        }
        previous_by_action: dict[str, StoredV1ExternalActionTransition] = {}
        actions: dict[str, StoredV1ExternalActionTransition] = {}
        for item in transitions:
            previous = previous_by_action.get(item.action_id)
            previous_state = None if previous is None else previous.state
            if item.state not in allowed.get(previous_state, set()):
                raise CheckpointCorruptionError("V1 external action state transition is invalid")
            binding = self.get_v1_intent_binding(runtime_id, item.client_order_id)
            if binding is None or binding.execution_intent_id != item.execution_intent_id:
                raise CheckpointCorruptionError("V1 external action authority binding is missing or mismatched")
            if previous is not None and (
                previous.action_kind, previous.attempt, previous.execution_intent_id,
                previous.client_order_id, previous.parent_action_id,
            ) != (
                item.action_kind, item.attempt, item.execution_intent_id, item.client_order_id, item.parent_action_id,
            ):
                raise CheckpointCorruptionError("V1 external action identity changed across journal transitions")
            if item.evidence_class != "SYNTHETIC_FIXTURE":
                raise CheckpointCorruptionError("V1 external action journal contains non-synthetic evidence")
            previous_by_action[item.action_id] = item
            actions[item.action_id] = item
        for item in actions.values():
            if item.parent_action_id is not None:
                parent = actions.get(item.parent_action_id)
                if parent is None or parent.state != "RESOLVED_ABSENT" or parent.attempt + 1 != item.attempt:
                    raise CheckpointCorruptionError("V1 external action retry lineage is invalid")
        return len(transitions)


    def append_v1_private_reconciliation(
        self,
        runtime_id: str,
        owner_id: str,
        *,
        reconciliation_id: str,
        venue_id: str,
        connection_id: str,
        completed_at: datetime,
        commissioning_ready: bool,
        report: dict[str, Any],
    ) -> int:
        if str(report.get("reconciliation", {}).get("reconciliation_id")) != reconciliation_id:
            raise ValueError("private reconciliation envelope does not match canonical report")
        if str(report.get("reconciliation", {}).get("venue_id")) != venue_id:
            raise ValueError("private reconciliation venue does not match canonical report")
        if str(report.get("reconciliation", {}).get("connection_id")) != connection_id:
            raise ValueError("private reconciliation connection does not match canonical report")
        if bool(report.get("commissioning_ready")) != bool(commissioning_ready):
            raise ValueError("private reconciliation commissioning state does not match canonical report")
        serialized = canonical_json(report)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot append V1 private reconciliation without runtime lease")
            existing = connection.execute(
                "SELECT sequence, venue_id, connection_id, completed_at, commissioning_ready, report_sha256 "
                "FROM execution_v1_private_reconciliations WHERE runtime_id=? AND reconciliation_id=?",
                (runtime_id, reconciliation_id),
            ).fetchone()
            if existing is not None:
                expected = (venue_id, connection_id, completed_at.isoformat(), int(commissioning_ready), digest)
                actual = (str(existing["venue_id"]), str(existing["connection_id"]), str(existing["completed_at"]), int(existing["commissioning_ready"]), str(existing["report_sha256"]))
                if actual != expected:
                    raise ValueError("conflicting V1 private reconciliation for reconciliation_id")
                return int(existing["sequence"])
            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM execution_v1_private_reconciliations WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()["next_sequence"])
            connection.execute(
                """INSERT INTO execution_v1_private_reconciliations(
                runtime_id, sequence, reconciliation_id, venue_id, connection_id, completed_at,
                commissioning_ready, report_json, report_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (runtime_id, sequence, reconciliation_id, venue_id, connection_id, completed_at.isoformat(),
                 int(commissioning_ready), serialized, digest),
            )
            connection.commit()
        return sequence

    def get_v1_private_reconciliation(self, runtime_id: str, reconciliation_id: str) -> StoredV1PrivateReconciliation | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v1_private_reconciliations WHERE runtime_id=? AND reconciliation_id=?",
                (runtime_id, reconciliation_id),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["report_json"])
        digest = sha256(raw.encode("utf-8")).hexdigest()
        if digest != str(row["report_sha256"]):
            raise CheckpointCorruptionError("V1 private reconciliation hash verification failed")
        return StoredV1PrivateReconciliation(
            sequence=int(row["sequence"]), reconciliation_id=str(row["reconciliation_id"]),
            venue_id=str(row["venue_id"]), connection_id=str(row["connection_id"]),
            completed_at=_parse_time(row["completed_at"]), commissioning_ready=bool(row["commissioning_ready"]),
            report=json.loads(raw), sha256=str(row["report_sha256"]),
        )

    def list_v1_private_reconciliations(self, runtime_id: str) -> tuple[StoredV1PrivateReconciliation, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT reconciliation_id FROM execution_v1_private_reconciliations WHERE runtime_id=? ORDER BY sequence",
                (runtime_id,),
            ).fetchall()
        result: list[StoredV1PrivateReconciliation] = []
        previous = 0
        for row in rows:
            item = self.get_v1_private_reconciliation(runtime_id, str(row["reconciliation_id"]))
            assert item is not None
            if item.sequence != previous + 1:
                raise CheckpointCorruptionError("V1 private reconciliation sequence is not contiguous")
            previous = item.sequence
            result.append(item)
        return tuple(result)

    def latest_v1_private_reconciliation(
        self, runtime_id: str, *, venue_id: str, connection_id: str
    ) -> StoredV1PrivateReconciliation | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT reconciliation_id FROM execution_v1_private_reconciliations "
                "WHERE runtime_id=? AND venue_id=? AND connection_id=? ORDER BY sequence DESC LIMIT 1",
                (runtime_id, venue_id, connection_id),
            ).fetchone()
        if row is None:
            return None
        return self.get_v1_private_reconciliation(runtime_id, str(row["reconciliation_id"]))

    def verify_v1_private_reconciliation_journal(self, runtime_id: str) -> int:
        values = self.list_v1_private_reconciliations(runtime_id)
        for item in values:
            report = item.report
            claimed_payload_hash = str(report.get("payload_hash", ""))
            base_report = dict(report)
            base_report.pop("payload_hash", None)
            if len(claimed_payload_hash) != 64 or payload_hash(base_report) != claimed_payload_hash:
                raise CheckpointCorruptionError("V1 private reconciliation canonical payload hash mismatch")
            canonical = report.get("reconciliation", {})
            if str(canonical.get("reconciliation_id")) != item.reconciliation_id:
                raise CheckpointCorruptionError("V1 private reconciliation identity mismatch")
            if str(canonical.get("venue_id")) != item.venue_id or str(canonical.get("connection_id")) != item.connection_id:
                raise CheckpointCorruptionError("V1 private reconciliation scope mismatch")
            if bool(report.get("commissioning_ready")) != item.commissioning_ready:
                raise CheckpointCorruptionError("V1 private reconciliation commissioning state mismatch")
            domains = report.get("domains")
            if not isinstance(domains, list):
                raise CheckpointCorruptionError("V1 private reconciliation domains missing")
            names = {str(domain.get("domain")) for domain in domains if isinstance(domain, dict)}
            required = {"ORDERS", "FILLS", "FEES", "BALANCES", "POSITIONS"}
            if names != required:
                raise CheckpointCorruptionError("V1 private reconciliation domain coverage incomplete")
            if item.commissioning_ready:
                if str(canonical.get("status")) != "MATCHED":
                    raise CheckpointCorruptionError("commissioning-ready private reconciliation is not MATCHED")
                if any(str(domain.get("state")) != "COMPLETE" for domain in domains):
                    raise CheckpointCorruptionError("commissioning-ready private reconciliation has incomplete truth")
        return len(values)

    def append_v1_private_drift(
        self,
        runtime_id: str,
        owner_id: str,
        *,
        drift_id: str,
        venue_id: str,
        connection_id: str,
        previous_reconciliation_id: str | None,
        current_reconciliation_id: str,
        detected_at: datetime,
        status: str,
        unresolved: bool,
        report: dict[str, Any],
    ) -> int:
        if str(report.get("drift_id")) != drift_id:
            raise ValueError("private drift envelope does not match drift id")
        if str(report.get("venue_id")) != venue_id or str(report.get("connection_id")) != connection_id:
            raise ValueError("private drift scope does not match report")
        if report.get("previous_reconciliation_id") != previous_reconciliation_id:
            raise ValueError("private drift previous reconciliation does not match report")
        if str(report.get("current_reconciliation_id")) != current_reconciliation_id:
            raise ValueError("private drift current reconciliation does not match report")
        if str(report.get("status")) != status or bool(report.get("unresolved")) != bool(unresolved):
            raise ValueError("private drift state does not match report")
        serialized = canonical_json(report)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot append V1 private drift without runtime lease")
            current = connection.execute(
                "SELECT venue_id, connection_id FROM execution_v1_private_reconciliations "
                "WHERE runtime_id=? AND reconciliation_id=?",
                (runtime_id, current_reconciliation_id),
            ).fetchone()
            if current is None or str(current["venue_id"]) != venue_id or str(current["connection_id"]) != connection_id:
                raise ValueError("private drift current reconciliation is missing or out of scope")
            if previous_reconciliation_id is not None:
                previous = connection.execute(
                    "SELECT venue_id, connection_id FROM execution_v1_private_reconciliations "
                    "WHERE runtime_id=? AND reconciliation_id=?",
                    (runtime_id, previous_reconciliation_id),
                ).fetchone()
                if previous is None or str(previous["venue_id"]) != venue_id or str(previous["connection_id"]) != connection_id:
                    raise ValueError("private drift previous reconciliation is missing or out of scope")
            existing = connection.execute(
                "SELECT sequence, report_sha256 FROM execution_v1_private_drift WHERE runtime_id=? AND drift_id=?",
                (runtime_id, drift_id),
            ).fetchone()
            if existing is not None:
                if str(existing["report_sha256"]) != digest:
                    raise ValueError("conflicting V1 private drift for drift_id")
                return int(existing["sequence"])
            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM execution_v1_private_drift WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()["next_sequence"])
            connection.execute(
                """INSERT INTO execution_v1_private_drift(
                runtime_id, sequence, drift_id, venue_id, connection_id, previous_reconciliation_id,
                current_reconciliation_id, detected_at, status, unresolved, report_json, report_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (runtime_id, sequence, drift_id, venue_id, connection_id, previous_reconciliation_id,
                 current_reconciliation_id, detected_at.isoformat(), status, int(unresolved), serialized, digest),
            )
            connection.commit()
        return sequence

    def get_v1_private_drift(self, runtime_id: str, drift_id: str) -> StoredV1PrivateDrift | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v1_private_drift WHERE runtime_id=? AND drift_id=?",
                (runtime_id, drift_id),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["report_json"])
        digest = sha256(raw.encode("utf-8")).hexdigest()
        if digest != str(row["report_sha256"]):
            raise CheckpointCorruptionError("V1 private drift hash verification failed")
        return StoredV1PrivateDrift(
            sequence=int(row["sequence"]), drift_id=str(row["drift_id"]),
            venue_id=str(row["venue_id"]), connection_id=str(row["connection_id"]),
            previous_reconciliation_id=None if row["previous_reconciliation_id"] is None else str(row["previous_reconciliation_id"]),
            current_reconciliation_id=str(row["current_reconciliation_id"]),
            detected_at=_parse_time(row["detected_at"]), status=str(row["status"]),
            unresolved=bool(row["unresolved"]), report=json.loads(raw), sha256=str(row["report_sha256"]),
        )

    def list_v1_private_drift(self, runtime_id: str) -> tuple[StoredV1PrivateDrift, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT drift_id FROM execution_v1_private_drift WHERE runtime_id=? ORDER BY sequence",
                (runtime_id,),
            ).fetchall()
        result: list[StoredV1PrivateDrift] = []
        previous = 0
        for row in rows:
            item = self.get_v1_private_drift(runtime_id, str(row["drift_id"]))
            assert item is not None
            if item.sequence != previous + 1:
                raise CheckpointCorruptionError("V1 private drift sequence is not contiguous")
            previous = item.sequence
            result.append(item)
        return tuple(result)

    def latest_v1_private_drift(
        self, runtime_id: str, *, venue_id: str, connection_id: str
    ) -> StoredV1PrivateDrift | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT drift_id FROM execution_v1_private_drift "
                "WHERE runtime_id=? AND venue_id=? AND connection_id=? ORDER BY sequence DESC LIMIT 1",
                (runtime_id, venue_id, connection_id),
            ).fetchone()
        if row is None:
            return None
        return self.get_v1_private_drift(runtime_id, str(row["drift_id"]))

    def verify_v1_private_drift_journal(self, runtime_id: str) -> int:
        values = self.list_v1_private_drift(runtime_id)
        prior_by_scope: dict[tuple[str, str], StoredV1PrivateDrift] = {}
        for item in values:
            report = item.report
            claimed_payload_hash = str(report.get("payload_hash", ""))
            base_report = dict(report)
            base_report.pop("payload_hash", None)
            if len(claimed_payload_hash) != 64 or payload_hash(base_report) != claimed_payload_hash:
                raise CheckpointCorruptionError("V1 private drift canonical payload hash mismatch")
            if str(report.get("drift_id")) != item.drift_id:
                raise CheckpointCorruptionError("V1 private drift identity mismatch")
            if str(report.get("venue_id")) != item.venue_id or str(report.get("connection_id")) != item.connection_id:
                raise CheckpointCorruptionError("V1 private drift scope mismatch")
            if report.get("previous_reconciliation_id") != item.previous_reconciliation_id:
                raise CheckpointCorruptionError("V1 private drift previous reconciliation mismatch")
            if str(report.get("current_reconciliation_id")) != item.current_reconciliation_id:
                raise CheckpointCorruptionError("V1 private drift current reconciliation mismatch")
            if str(report.get("status")) != item.status or bool(report.get("unresolved")) != item.unresolved:
                raise CheckpointCorruptionError("V1 private drift state mismatch")
            domains = report.get("domains")
            if not isinstance(domains, list):
                raise CheckpointCorruptionError("V1 private drift domains missing")
            names = {str(domain.get("domain")) for domain in domains if isinstance(domain, dict)}
            if names != {"ORDERS", "FILLS", "FEES", "BALANCES", "POSITIONS"}:
                raise CheckpointCorruptionError("V1 private drift domain coverage incomplete")
            if item.status in {"DRIFT_DETECTED", "BLOCKED_UNKNOWN"} and not item.unresolved:
                raise CheckpointCorruptionError("unresolved V1 private drift marked clear")
            if item.status in {"BASELINE_ESTABLISHED", "STABLE", "DRIFT_RESOLVED"} and item.unresolved:
                raise CheckpointCorruptionError("resolved V1 private drift marked unresolved")
            current = self.get_v1_private_reconciliation(runtime_id, item.current_reconciliation_id)
            if current is None or current.venue_id != item.venue_id or current.connection_id != item.connection_id:
                raise CheckpointCorruptionError("V1 private drift current reconciliation missing")
            scope = (item.venue_id, item.connection_id)
            prior = prior_by_scope.get(scope)
            previous_reconciliation = None
            if prior is None:
                if item.previous_reconciliation_id is not None:
                    previous_reconciliation = self.get_v1_private_reconciliation(runtime_id, item.previous_reconciliation_id)
                    if (
                        previous_reconciliation is None
                        or previous_reconciliation.venue_id != item.venue_id
                        or previous_reconciliation.connection_id != item.connection_id
                    ):
                        raise CheckpointCorruptionError("V1 private drift legacy reconciliation anchor is missing or out of scope")
            else:
                if item.previous_reconciliation_id != prior.current_reconciliation_id:
                    raise CheckpointCorruptionError("V1 private drift reconciliation lineage is not contiguous")
                previous_reconciliation = self.get_v1_private_reconciliation(runtime_id, prior.current_reconciliation_id)
                if previous_reconciliation is None:
                    raise CheckpointCorruptionError("V1 private drift previous reconciliation missing")

            from quant_system.execution.v1.drift import DriftStatus, compare_private_read_domains
            expected_domains = compare_private_read_domains(
                None if previous_reconciliation is None else previous_reconciliation.report, current.report
            )
            supplied_by_domain = {str(domain.get("domain")): domain for domain in domains if isinstance(domain, dict)}
            for expected_domain in expected_domains:
                supplied = supplied_by_domain.get(expected_domain.domain.value)
                if supplied != expected_domain.to_payload():
                    raise CheckpointCorruptionError("V1 private drift domain comparison mismatch")
            changed = any(domain.changed for domain in expected_domains)
            current_canonical = current.report.get("reconciliation", {})
            current_matched = current.commissioning_ready and str(current_canonical.get("status")) == "MATCHED"
            if not current_matched:
                expected_status, expected_unresolved = DriftStatus.BLOCKED_UNKNOWN.value, True
            elif previous_reconciliation is None:
                expected_status, expected_unresolved = DriftStatus.BASELINE_ESTABLISHED.value, False
            elif changed:
                expected_status, expected_unresolved = DriftStatus.DRIFT_DETECTED.value, True
            elif prior is not None and prior.unresolved:
                expected_status, expected_unresolved = DriftStatus.DRIFT_RESOLVED.value, False
            else:
                expected_status, expected_unresolved = DriftStatus.STABLE.value, False
            if item.status != expected_status or item.unresolved != expected_unresolved:
                raise CheckpointCorruptionError("V1 private drift semantic state mismatch")
            prior_by_scope[scope] = item
        return len(values)

    def bind_v1_reconciliation_policy(
        self, runtime_id: str, owner_id: str, *, policy: dict[str, Any]
    ) -> StoredV1ReconciliationPolicy:
        policy_id = str(policy.get("policy_id", ""))
        venue_id = str(policy.get("venue_id", ""))
        connection_id = str(policy.get("connection_id", ""))
        cadence_seconds = int(policy.get("cadence_seconds", 0))
        maximum_age_seconds = int(policy.get("maximum_age_seconds", 0))
        anchor_at = _parse_time(str(policy.get("anchor_at")))
        trading_capable = bool(policy.get("trading_capable"))
        if not policy_id or not venue_id or not connection_id or anchor_at is None:
            raise ValueError("reconciliation watchdog policy identity is incomplete")
        if cadence_seconds <= 0 or maximum_age_seconds < cadence_seconds:
            raise ValueError("invalid reconciliation watchdog cadence policy")
        claimed = str(policy.get("payload_hash", ""))
        base = dict(policy)
        base.pop("payload_hash", None)
        if len(claimed) != 64 or payload_hash(base) != claimed:
            raise ValueError("reconciliation watchdog policy canonical payload hash mismatch")
        serialized = canonical_json(policy)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot bind reconciliation watchdog policy without runtime lease")
            existing_scope = connection.execute(
                "SELECT * FROM execution_v1_reconciliation_policies WHERE runtime_id=? AND venue_id=? AND connection_id=?",
                (runtime_id, venue_id, connection_id),
            ).fetchone()
            if existing_scope is not None:
                if str(existing_scope["policy_id"]) != policy_id or str(existing_scope["policy_sha256"]) != digest:
                    raise ValueError("conflicting reconciliation watchdog policy for venue/connection scope")
                return StoredV1ReconciliationPolicy(
                    policy_id=policy_id, venue_id=venue_id, connection_id=connection_id,
                    cadence_seconds=cadence_seconds, maximum_age_seconds=maximum_age_seconds,
                    anchor_at=anchor_at, trading_capable=trading_capable, policy=json.loads(str(existing_scope["policy_json"])),
                    sha256=str(existing_scope["policy_sha256"]),
                )
            connection.execute(
                """INSERT INTO execution_v1_reconciliation_policies(
                runtime_id, policy_id, venue_id, connection_id, cadence_seconds, maximum_age_seconds,
                anchor_at, trading_capable, policy_json, policy_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (runtime_id, policy_id, venue_id, connection_id, cadence_seconds, maximum_age_seconds,
                 anchor_at.isoformat(), int(trading_capable), serialized, digest),
            )
            connection.commit()
        return StoredV1ReconciliationPolicy(
            policy_id=policy_id, venue_id=venue_id, connection_id=connection_id,
            cadence_seconds=cadence_seconds, maximum_age_seconds=maximum_age_seconds,
            anchor_at=anchor_at, trading_capable=trading_capable, policy=policy, sha256=digest,
        )

    def get_v1_reconciliation_policy(
        self, runtime_id: str, *, venue_id: str, connection_id: str
    ) -> StoredV1ReconciliationPolicy | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v1_reconciliation_policies WHERE runtime_id=? AND venue_id=? AND connection_id=?",
                (runtime_id, venue_id, connection_id),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["policy_json"])
        if sha256(raw.encode("utf-8")).hexdigest() != str(row["policy_sha256"]):
            raise CheckpointCorruptionError("V1 reconciliation watchdog policy hash verification failed")
        return StoredV1ReconciliationPolicy(
            policy_id=str(row["policy_id"]), venue_id=str(row["venue_id"]), connection_id=str(row["connection_id"]),
            cadence_seconds=int(row["cadence_seconds"]), maximum_age_seconds=int(row["maximum_age_seconds"]),
            anchor_at=_parse_time(row["anchor_at"]), trading_capable=bool(row["trading_capable"]),
            policy=json.loads(raw), sha256=str(row["policy_sha256"]),
        )

    def list_v1_reconciliation_policies(self, runtime_id: str) -> tuple[StoredV1ReconciliationPolicy, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT venue_id, connection_id FROM execution_v1_reconciliation_policies WHERE runtime_id=? ORDER BY venue_id, connection_id",
                (runtime_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = self.get_v1_reconciliation_policy(runtime_id, venue_id=str(row["venue_id"]), connection_id=str(row["connection_id"]))
            assert item is not None
            result.append(item)
        return tuple(result)

    def append_v1_reconciliation_watchdog(
        self, runtime_id: str, owner_id: str, *, generation: int, assessment: dict[str, Any]
    ) -> int:
        assessment_id = str(assessment.get("assessment_id", ""))
        policy_id = str(assessment.get("policy_id", ""))
        venue_id = str(assessment.get("venue_id", ""))
        connection_id = str(assessment.get("connection_id", ""))
        assessed_at = _parse_time(str(assessment.get("assessed_at")))
        next_due_at = _parse_time(str(assessment.get("next_due_at")))
        last_reconciliation_id = assessment.get("last_reconciliation_id")
        status = str(assessment.get("status", ""))
        commissioning_hold = bool(assessment.get("commissioning_hold"))
        schema_version = str(assessment.get("schema_version", ""))
        if generation <= 0:
            raise RuntimeLeaseError("reconciliation watchdog append requires a positive runtime generation")
        if not assessment_id or not policy_id or not venue_id or not connection_id or assessed_at is None or next_due_at is None:
            raise ValueError("reconciliation watchdog assessment identity is incomplete")
        claimed = str(assessment.get("payload_hash", ""))
        base = dict(assessment)
        base.pop("payload_hash", None)
        if len(claimed) != 64 or payload_hash(base) != claimed:
            raise ValueError("reconciliation watchdog canonical payload hash mismatch")
        serialized = canonical_json(assessment)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease = connection.execute(
                "SELECT owner_id, generation, lease_expires_at FROM runtime_state WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()
            if lease is None or lease["owner_id"] != owner_id or int(lease["generation"]) != generation:
                raise RuntimeLeaseError("cannot append reconciliation watchdog assessment without current runtime lease generation")
            lease_expires_at = _parse_time(lease["lease_expires_at"])
            if lease_expires_at is None or assessed_at > lease_expires_at:
                raise RuntimeLeaseError("cannot append reconciliation watchdog assessment with expired runtime lease")
            policy = connection.execute(
                "SELECT venue_id, connection_id FROM execution_v1_reconciliation_policies WHERE runtime_id=? AND policy_id=?",
                (runtime_id, policy_id),
            ).fetchone()
            if policy is None or str(policy["venue_id"]) != venue_id or str(policy["connection_id"]) != connection_id:
                raise ValueError("reconciliation watchdog policy is missing or out of scope")
            if last_reconciliation_id is not None:
                recon = connection.execute(
                    "SELECT venue_id, connection_id FROM execution_v1_private_reconciliations WHERE runtime_id=? AND reconciliation_id=?",
                    (runtime_id, str(last_reconciliation_id)),
                ).fetchone()
                if recon is None or str(recon["venue_id"]) != venue_id or str(recon["connection_id"]) != connection_id:
                    raise ValueError("reconciliation watchdog references missing or out-of-scope reconciliation")

            existing = connection.execute(
                "SELECT sequence, report_sha256 FROM execution_v1_reconciliation_watchdog WHERE runtime_id=? AND assessment_id=?",
                (runtime_id, assessment_id),
            ).fetchone()
            if existing is not None:
                if str(existing["report_sha256"]) != digest:
                    raise ValueError("conflicting reconciliation watchdog assessment")
                return int(existing["sequence"])

            head = connection.execute(
                "SELECT assessment_id, sequence FROM execution_v1_reconciliation_watchdog_heads "
                "WHERE runtime_id=? AND venue_id=? AND connection_id=?",
                (runtime_id, venue_id, connection_id),
            ).fetchone()
            expected_predecessor = None if head is None else str(head["assessment_id"])
            if schema_version == "EQS-EXEC-RECON-WATCHDOG-v1.2":
                actual_predecessor = assessment.get("predecessor_assessment_id")
                actual_predecessor = None if actual_predecessor is None else str(actual_predecessor)
                if actual_predecessor != expected_predecessor:
                    raise WatchdogHeadConflictError("reconciliation watchdog head compare-and-swap predecessor mismatch")
            else:
                raise WatchdogHeadConflictError("new reconciliation watchdog writes require current v1.2 head-fenced schema")

            sequence = int(connection.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM execution_v1_reconciliation_watchdog WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()["next_sequence"])
            connection.execute(
                """INSERT INTO execution_v1_reconciliation_watchdog(
                runtime_id, sequence, assessment_id, policy_id, venue_id, connection_id, assessed_at,
                last_reconciliation_id, next_due_at, status, commissioning_hold, report_json, report_sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (runtime_id, sequence, assessment_id, policy_id, venue_id, connection_id, assessed_at.isoformat(),
                 last_reconciliation_id, next_due_at.isoformat(), status, int(commissioning_hold), serialized, digest),
            )

            if head is None:
                try:
                    connection.execute(
                        """INSERT INTO execution_v1_reconciliation_watchdog_heads(
                        runtime_id, venue_id, connection_id, assessment_id, sequence, writer_owner_id, writer_generation, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (runtime_id, venue_id, connection_id, assessment_id, sequence, owner_id, generation, assessed_at.isoformat()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise WatchdogHeadConflictError("reconciliation watchdog head compare-and-swap lost first-writer race") from exc
            else:
                cursor = connection.execute(
                    """UPDATE execution_v1_reconciliation_watchdog_heads
                    SET assessment_id=?, sequence=?, writer_owner_id=?, writer_generation=?, updated_at=?
                    WHERE runtime_id=? AND venue_id=? AND connection_id=?
                      AND assessment_id=? AND sequence=?""",
                    (assessment_id, sequence, owner_id, generation, assessed_at.isoformat(),
                     runtime_id, venue_id, connection_id, expected_predecessor, int(head["sequence"])),
                )
                if cursor.rowcount != 1:
                    raise WatchdogHeadConflictError("reconciliation watchdog head compare-and-swap lost successor race")
            connection.commit()
        return sequence

    def get_v1_reconciliation_watchdog_head(
        self, runtime_id: str, *, venue_id: str, connection_id: str
    ) -> StoredV1ReconciliationWatchdogHead | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v1_reconciliation_watchdog_heads "
                "WHERE runtime_id=? AND venue_id=? AND connection_id=?",
                (runtime_id, venue_id, connection_id),
            ).fetchone()
        if row is None:
            return None
        return StoredV1ReconciliationWatchdogHead(
            venue_id=str(row["venue_id"]), connection_id=str(row["connection_id"]),
            assessment_id=str(row["assessment_id"]), sequence=int(row["sequence"]),
            writer_owner_id=str(row["writer_owner_id"]), writer_generation=int(row["writer_generation"]),
            updated_at=_parse_time(str(row["updated_at"])),
        )

    def list_v1_reconciliation_watchdog_heads(
        self, runtime_id: str
    ) -> tuple[StoredV1ReconciliationWatchdogHead, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT venue_id, connection_id FROM execution_v1_reconciliation_watchdog_heads "
                "WHERE runtime_id=? ORDER BY venue_id, connection_id",
                (runtime_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = self.get_v1_reconciliation_watchdog_head(
                runtime_id, venue_id=str(row["venue_id"]), connection_id=str(row["connection_id"])
            )
            assert item is not None
            result.append(item)
        return tuple(result)

    def get_v1_reconciliation_watchdog(self, runtime_id: str, assessment_id: str) -> StoredV1ReconciliationWatchdog | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM execution_v1_reconciliation_watchdog WHERE runtime_id=? AND assessment_id=?",
                (runtime_id, assessment_id),
            ).fetchone()
        if row is None:
            return None
        raw = str(row["report_json"])
        if sha256(raw.encode("utf-8")).hexdigest() != str(row["report_sha256"]):
            raise CheckpointCorruptionError("V1 reconciliation watchdog hash verification failed")
        return StoredV1ReconciliationWatchdog(
            sequence=int(row["sequence"]), assessment_id=str(row["assessment_id"]), policy_id=str(row["policy_id"]),
            venue_id=str(row["venue_id"]), connection_id=str(row["connection_id"]), assessed_at=_parse_time(row["assessed_at"]),
            last_reconciliation_id=None if row["last_reconciliation_id"] is None else str(row["last_reconciliation_id"]),
            next_due_at=_parse_time(row["next_due_at"]), status=str(row["status"]),
            commissioning_hold=bool(row["commissioning_hold"]), report=json.loads(raw), sha256=str(row["report_sha256"]),
        )

    def list_v1_reconciliation_watchdog(self, runtime_id: str) -> tuple[StoredV1ReconciliationWatchdog, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT assessment_id FROM execution_v1_reconciliation_watchdog WHERE runtime_id=? ORDER BY sequence",
                (runtime_id,),
            ).fetchall()
        result = []
        previous = 0
        for row in rows:
            item = self.get_v1_reconciliation_watchdog(runtime_id, str(row["assessment_id"]))
            assert item is not None
            if item.sequence != previous + 1:
                raise CheckpointCorruptionError("V1 reconciliation watchdog sequence is not contiguous")
            previous = item.sequence
            result.append(item)
        return tuple(result)

    def latest_v1_reconciliation_watchdog(
        self, runtime_id: str, *, venue_id: str, connection_id: str
    ) -> StoredV1ReconciliationWatchdog | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT assessment_id FROM execution_v1_reconciliation_watchdog WHERE runtime_id=? AND venue_id=? AND connection_id=? ORDER BY sequence DESC LIMIT 1",
                (runtime_id, venue_id, connection_id),
            ).fetchone()
        if row is None:
            return None
        return self.get_v1_reconciliation_watchdog(runtime_id, str(row["assessment_id"]))

    def verify_v1_reconciliation_watchdog_journal(self, runtime_id: str) -> int:
        from quant_system.execution.v1.watchdog import (
            CURRENT_WATCHDOG_SCHEMA_VERSION, HYSTERESIS_WATCHDOG_SCHEMA_VERSION, LEGACY_WATCHDOG_SCHEMA_VERSION,
            ReconciliationCadencePolicy, advance_recovery_context, assess_reconciliation_watchdog,
            deterministic_policy_id, hysteresis_v1_1_watchdog_payload, legacy_reconciliation_watchdog_payload,
        )
        policies = self.list_v1_reconciliation_policies(runtime_id)
        policy_by_id = {}
        for item in policies:
            payload = item.policy
            claimed = str(payload.get("payload_hash", ""))
            base = dict(payload)
            base.pop("payload_hash", None)
            if len(claimed) != 64 or payload_hash(base) != claimed:
                raise CheckpointCorruptionError("V1 reconciliation watchdog policy canonical payload hash mismatch")
            if str(payload.get("policy_id")) != item.policy_id or str(payload.get("venue_id")) != item.venue_id or str(payload.get("connection_id")) != item.connection_id:
                raise CheckpointCorruptionError("V1 reconciliation watchdog policy identity mismatch")
            if (
                int(payload.get("cadence_seconds", 0)) != item.cadence_seconds
                or int(payload.get("maximum_age_seconds", 0)) != item.maximum_age_seconds
                or _parse_time(str(payload.get("anchor_at"))) != item.anchor_at
                or bool(payload.get("trading_capable")) != item.trading_capable
            ):
                raise CheckpointCorruptionError("V1 reconciliation watchdog policy semantic mismatch")
            expected_policy_id = deterministic_policy_id(
                venue_id=item.venue_id, connection_id=item.connection_id,
                cadence_seconds=item.cadence_seconds, maximum_age_seconds=item.maximum_age_seconds,
                anchor_at=item.anchor_at, trading_capable=item.trading_capable,
            )
            if item.policy_id != expected_policy_id:
                raise CheckpointCorruptionError("V1 reconciliation watchdog policy deterministic identity mismatch")
            policy_by_id[item.policy_id] = ReconciliationCadencePolicy(
                policy_id=item.policy_id, venue_id=item.venue_id, connection_id=item.connection_id,
                cadence_seconds=item.cadence_seconds, maximum_age_seconds=item.maximum_age_seconds,
                anchor_at=item.anchor_at, trading_capable=item.trading_capable,
            )
        values = self.list_v1_reconciliation_watchdog(runtime_id)
        previous_by_scope: dict[tuple[str, str], object] = {}
        for item in values:
            report = item.report
            if str(report.get("assessment_id", "")) != item.assessment_id:
                raise CheckpointCorruptionError("V1 reconciliation watchdog assessment identity mismatch")
            if str(report.get("policy_id", "")) != item.policy_id:
                raise CheckpointCorruptionError("V1 reconciliation watchdog policy identity mismatch")
            if str(report.get("venue_id", "")) != item.venue_id or str(report.get("connection_id", "")) != item.connection_id:
                raise CheckpointCorruptionError("V1 reconciliation watchdog scope identity mismatch")
            if _parse_time(str(report.get("assessed_at"))) != item.assessed_at:
                raise CheckpointCorruptionError("V1 reconciliation watchdog assessed-at envelope mismatch")
            report_reconciliation_id = None if report.get("last_reconciliation_id") is None else str(report.get("last_reconciliation_id"))
            if report_reconciliation_id != item.last_reconciliation_id:
                raise CheckpointCorruptionError("V1 reconciliation watchdog reconciliation envelope mismatch")
            claimed = str(report.get("payload_hash", ""))
            base = dict(report)
            base.pop("payload_hash", None)
            if len(claimed) != 64 or payload_hash(base) != claimed:
                raise CheckpointCorruptionError("V1 reconciliation watchdog canonical payload hash mismatch")
            policy = policy_by_id.get(item.policy_id)
            if policy is None:
                raise CheckpointCorruptionError("V1 reconciliation watchdog policy missing")
            recon = None if item.last_reconciliation_id is None else self.get_v1_private_reconciliation(runtime_id, item.last_reconciliation_id)
            scope = (item.venue_id, item.connection_id)
            previous = previous_by_scope.get(scope)
            if previous is not None and item.assessed_at < previous.assessed_at:
                raise CheckpointCorruptionError("V1 reconciliation watchdog assessment time is not monotonic")
            schema_version = str(report.get("schema_version", ""))
            supported_versions = {LEGACY_WATCHDOG_SCHEMA_VERSION, HYSTERESIS_WATCHDOG_SCHEMA_VERSION, CURRENT_WATCHDOG_SCHEMA_VERSION}
            if schema_version not in supported_versions:
                raise CheckpointCorruptionError("V1 reconciliation watchdog unsupported schema version")
            version_rank = {LEGACY_WATCHDOG_SCHEMA_VERSION: 0, HYSTERESIS_WATCHDOG_SCHEMA_VERSION: 1, CURRENT_WATCHDOG_SCHEMA_VERSION: 2}
            if previous is not None and version_rank[schema_version] < version_rank[previous.schema_version]:
                raise CheckpointCorruptionError("V1 reconciliation watchdog schema downgrade")
            if schema_version == CURRENT_WATCHDOG_SCHEMA_VERSION:
                expected_predecessor = None if previous is None else previous.assessment_id
                actual_predecessor = None if report.get("predecessor_assessment_id") is None else str(report.get("predecessor_assessment_id"))
                if actual_predecessor != expected_predecessor:
                    raise CheckpointCorruptionError("V1 reconciliation watchdog predecessor branch mismatch")
            if schema_version == LEGACY_WATCHDOG_SCHEMA_VERSION:
                expected_payload = legacy_reconciliation_watchdog_payload(
                    policy, assessed_at=item.assessed_at,
                    last_reconciliation_id=None if recon is None else recon.reconciliation_id,
                    last_reconciliation_completed_at=None if recon is None else recon.completed_at,
                )
                expected_status = str(expected_payload["status"])
                expected_hold = bool(expected_payload["commissioning_hold"])
                expected_due = _parse_time(str(expected_payload["next_due_at"]))
            elif schema_version == HYSTERESIS_WATCHDOG_SCHEMA_VERSION:
                expected_payload = hysteresis_v1_1_watchdog_payload(
                    policy, assessed_at=item.assessed_at,
                    last_reconciliation_id=None if recon is None else recon.reconciliation_id,
                    last_reconciliation_completed_at=None if recon is None else recon.completed_at,
                    last_reconciliation_complete=False if recon is None else recon.commissioning_ready,
                    previous_assessment=previous,
                )
                expected_status = str(expected_payload["status"])
                expected_hold = bool(expected_payload["commissioning_hold"])
                expected_due = _parse_time(str(expected_payload["next_due_at"]))
            else:
                expected = assess_reconciliation_watchdog(
                    policy, assessed_at=item.assessed_at,
                    last_reconciliation_id=None if recon is None else recon.reconciliation_id,
                    last_reconciliation_completed_at=None if recon is None else recon.completed_at,
                    last_reconciliation_complete=False if recon is None else recon.commissioning_ready,
                    previous_assessment=previous,
                )
                expected_payload = expected.to_payload()
                expected_status = expected.status.value
                expected_hold = expected.commissioning_hold
                expected_due = expected.next_due_at
            if report != expected_payload:
                raise CheckpointCorruptionError("V1 reconciliation watchdog semantic assessment mismatch")
            if item.status != expected_status or item.commissioning_hold != expected_hold or item.next_due_at != expected_due:
                raise CheckpointCorruptionError("V1 reconciliation watchdog envelope mismatch")
            try:
                previous_by_scope[scope] = advance_recovery_context(previous, report)
            except ValueError as exc:
                raise CheckpointCorruptionError(str(exc)) from exc

        latest_by_scope: dict[tuple[str, str], StoredV1ReconciliationWatchdog] = {}
        for item in values:
            latest_by_scope[(item.venue_id, item.connection_id)] = item
        heads = self.list_v1_reconciliation_watchdog_heads(runtime_id)
        head_by_scope = {(item.venue_id, item.connection_id): item for item in heads}
        if set(head_by_scope) != set(latest_by_scope):
            raise CheckpointCorruptionError("V1 reconciliation watchdog head scope set mismatch")
        runtime = self.get_runtime(runtime_id)
        current_generation = 0 if runtime is None else runtime.generation
        for scope, latest in latest_by_scope.items():
            head = head_by_scope[scope]
            if head.assessment_id != latest.assessment_id or head.sequence != latest.sequence:
                raise CheckpointCorruptionError("V1 reconciliation watchdog head does not match canonical branch tip")
            if head.updated_at != latest.assessed_at:
                raise CheckpointCorruptionError("V1 reconciliation watchdog head timestamp mismatch")
            if head.writer_generation < 0 or head.writer_generation > current_generation:
                raise CheckpointCorruptionError("V1 reconciliation watchdog head writer generation is invalid")
            if head.writer_generation == 0:
                if head.writer_owner_id != "MIGRATED":
                    raise CheckpointCorruptionError("V1 reconciliation watchdog migrated head authority marker mismatch")
            elif not head.writer_owner_id.strip():
                raise CheckpointCorruptionError("V1 reconciliation watchdog head writer owner is missing")
        return len(values)

    def save_checkpoint(self, runtime_id: str, owner_id: str, *, generation: int, created_at: datetime, payload: dict[str, Any]) -> StoredCheckpoint:
        """Persist bounded restart state while retaining sparse lifecycle archives."""
        serialized = canonical_json(payload)
        digest = sha256(serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            lease = connection.execute("SELECT owner_id FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id:
                raise RuntimeLeaseError("cannot checkpoint without runtime lease")

            head = connection.execute(
                "SELECT id, archive_checkpoint_id FROM runtime_checkpoint_heads WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()
            if head is None:
                cursor = connection.execute(
                    """INSERT INTO runtime_checkpoint_heads(
                    runtime_id, generation, created_at, payload_json, payload_sha256, archive_checkpoint_id
                    ) VALUES (?, ?, ?, ?, ?, NULL)""",
                    (runtime_id, generation, created_at.isoformat(), serialized, digest),
                )
                checkpoint_id = int(cursor.lastrowid)
            else:
                checkpoint_id = int(head["id"])
                connection.execute(
                    """UPDATE runtime_checkpoint_heads
                    SET generation=?, created_at=?, payload_json=?, payload_sha256=?, archive_checkpoint_id=NULL
                    WHERE runtime_id=?""",
                    (generation, created_at.isoformat(), serialized, digest, runtime_id),
                )

            latest_archive = connection.execute(
                """SELECT generation, created_at, payload_sha256
                FROM runtime_checkpoints WHERE runtime_id=? ORDER BY id DESC LIMIT 1""",
                (runtime_id,),
            ).fetchone()
            status = str(payload.get("status") or "")
            archive_required = (
                latest_archive is None
                or int(latest_archive["generation"]) != int(generation)
                or (
                    status in {"HALTED", "STOPPED"}
                    and str(latest_archive["payload_sha256"]) != digest
                )
            )
            if archive_required:
                archive_cursor = connection.execute(
                    """INSERT INTO runtime_checkpoints(
                    runtime_id, generation, created_at, payload_json, payload_sha256
                    ) VALUES (?, ?, ?, ?, ?)""",
                    (runtime_id, generation, created_at.isoformat(), serialized, digest),
                )
                archive_checkpoint_id = int(archive_cursor.lastrowid)
                connection.execute(
                    "UPDATE runtime_checkpoint_heads SET archive_checkpoint_id=? WHERE runtime_id=?",
                    (archive_checkpoint_id, runtime_id),
                )
        return StoredCheckpoint(checkpoint_id, generation, created_at, payload, digest)

    def latest_checkpoint(self, runtime_id: str) -> StoredCheckpoint | None:
        with self._connection() as connection:
            head_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_checkpoint_heads'"
            ).fetchone() is not None
            row = None
            if head_exists:
                row = connection.execute(
                    "SELECT * FROM runtime_checkpoint_heads WHERE runtime_id=?",
                    (runtime_id,),
                ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM runtime_checkpoints WHERE runtime_id=? ORDER BY id DESC LIMIT 1",
                    (runtime_id,),
                ).fetchone()
            if row is None:
                return None

            payload = json.loads(row["payload_json"])
            actual = payload_hash(payload)
            if actual != row["payload_sha256"]:
                raise CheckpointCorruptionError("runtime checkpoint hash verification failed")

            if head_exists and "archive_checkpoint_id" in row.keys() and row["archive_checkpoint_id"] is not None:
                archive = connection.execute(
                    "SELECT * FROM runtime_checkpoints WHERE id=? AND runtime_id=?",
                    (int(row["archive_checkpoint_id"]), runtime_id),
                ).fetchone()
                if archive is None:
                    raise CheckpointCorruptionError("runtime checkpoint head archive reference is missing")
                archive_payload = json.loads(archive["payload_json"])
                archive_actual = payload_hash(archive_payload)
                if archive_actual != archive["payload_sha256"]:
                    raise CheckpointCorruptionError("archived runtime checkpoint hash verification failed")
                if archive_actual != actual:
                    raise CheckpointCorruptionError("runtime checkpoint head/archive mismatch")

        return StoredCheckpoint(
            int(row["id"]),
            int(row["generation"]),
            _parse_time(row["created_at"]),
            payload,
            str(row["payload_sha256"]),
        )

    def get_runtime(self, runtime_id: str) -> RuntimeRecord | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
        if row is None:
            return None
        return RuntimeRecord(
            runtime_id=row["runtime_id"],
            mode=row["mode"],
            status=row["status"],
            owner_id=row["owner_id"],
            generation=int(row["generation"]),
            lease_expires_at=_parse_time(row["lease_expires_at"]),
            halt_reason=row["halt_reason"],
            created_at=_parse_time(row["created_at"]),
            updated_at=_parse_time(row["updated_at"]),
            last_heartbeat_at=_parse_time(row["last_heartbeat_at"]),
        )

    def load_events(self, runtime_id: str) -> tuple[StoredRuntimeEvent, ...]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence, event_type, occurred_at, payload_json, payload_sha256 FROM runtime_events WHERE runtime_id=? ORDER BY sequence",
                (runtime_id,),
            ).fetchall()
        events: list[StoredRuntimeEvent] = []
        previous = 0
        for row in rows:
            sequence = int(row["sequence"])
            if sequence != previous + 1:
                raise CheckpointCorruptionError("runtime event sequence is not contiguous")
            previous = sequence
            digest = sha256(row["payload_json"].encode("utf-8")).hexdigest()
            if digest != row["payload_sha256"]:
                raise CheckpointCorruptionError("runtime event hash verification failed")
            events.append(StoredRuntimeEvent(
                sequence=sequence,
                event_type=str(row["event_type"]),
                occurred_at=_parse_time(row["occurred_at"]),
                payload=json.loads(row["payload_json"]),
                sha256=str(row["payload_sha256"]),
            ))
        return tuple(events)

    def event_count(self, runtime_id: str, event_type: str | None = None) -> int:
        with self._connection() as connection:
            if event_type is None:
                row = connection.execute("SELECT COUNT(*) AS n FROM runtime_events WHERE runtime_id=?", (runtime_id,)).fetchone()
            else:
                row = connection.execute(
                    "SELECT COUNT(*) AS n FROM runtime_events WHERE runtime_id=? AND event_type=?", (runtime_id, event_type)
                ).fetchone()
        return int(row["n"])

    def verify_events(self, runtime_id: str) -> int:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence, payload_json, payload_sha256 FROM runtime_events WHERE runtime_id=? ORDER BY sequence", (runtime_id,)
            ).fetchall()
        previous = 0
        for row in rows:
            if int(row["sequence"]) != previous + 1:
                raise CheckpointCorruptionError("runtime event sequence is not contiguous")
            previous += 1
            digest = sha256(row["payload_json"].encode("utf-8")).hexdigest()
            if digest != row["payload_sha256"]:
                raise CheckpointCorruptionError("runtime event hash verification failed")
        return previous

    def _test_fault(self, point: str) -> None:
        """Explicit test seam. Production construction leaves this unset."""
        if self._test_fault_injector is not None:
            self._test_fault_injector(point)

    def append_event_and_checkpoint(self, runtime_id: str, owner_id: str, *, event_type: str, occurred_at: datetime, event_payload: dict[str, Any], generation: int, checkpoint_payload: dict[str, Any], v1_settlements: list[dict[str, Any]] | None = None) -> StoredCheckpoint:
        """Atomically commit an event receipt and the exact recovery state it describes."""
        event_serialized = canonical_json(event_payload)
        event_digest = sha256(event_serialized.encode("utf-8")).hexdigest()
        checkpoint_serialized = canonical_json(checkpoint_payload)
        checkpoint_digest = sha256(checkpoint_serialized.encode("utf-8")).hexdigest()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._test_fault("AFTER_BEGIN")
            lease = connection.execute("SELECT owner_id, generation, lease_expires_at FROM runtime_state WHERE runtime_id=?", (runtime_id,)).fetchone()
            if lease is None or lease["owner_id"] != owner_id or int(lease["generation"]) != generation:
                raise RuntimeLeaseError("cannot atomically commit without runtime lease")
            expiry = _parse_time(lease["lease_expires_at"])
            if expiry is None or occurred_at > expiry:
                raise RuntimeLeaseError("cannot atomically commit with expired lease")
            for item in v1_settlements or []:
                self._append_v1_settlement_in_transaction(connection, runtime_id, owner_id, **item)
            self._test_fault("AFTER_V1_SETTLEMENTS")
            sequence = connection.execute("SELECT COALESCE(MAX(sequence),0)+1 AS next_sequence FROM runtime_events WHERE runtime_id=?", (runtime_id,)).fetchone()["next_sequence"]
            self._test_fault("BEFORE_EVENT_INSERT")
            connection.execute("INSERT INTO runtime_events(runtime_id, sequence, event_type, occurred_at, payload_json, payload_sha256) VALUES (?, ?, ?, ?, ?, ?)", (runtime_id, sequence, event_type, occurred_at.isoformat(), event_serialized, event_digest))
            self._test_fault("AFTER_EVENT_INSERT")
            head = connection.execute(
                "SELECT id FROM runtime_checkpoint_heads WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()
            if head is None:
                cursor = connection.execute(
                    """INSERT INTO runtime_checkpoint_heads(
                    runtime_id, generation, created_at, payload_json, payload_sha256, archive_checkpoint_id
                    ) VALUES (?, ?, ?, ?, ?, NULL)""",
                    (runtime_id, generation, occurred_at.isoformat(), checkpoint_serialized, checkpoint_digest),
                )
                checkpoint_id = int(cursor.lastrowid)
            else:
                checkpoint_id = int(head["id"])
                connection.execute(
                    """UPDATE runtime_checkpoint_heads
                    SET generation=?, created_at=?, payload_json=?, payload_sha256=?, archive_checkpoint_id=NULL
                    WHERE runtime_id=?""",
                    (generation, occurred_at.isoformat(), checkpoint_serialized, checkpoint_digest, runtime_id),
                )
            self._test_fault("AFTER_CHECKPOINT_INSERT")
            connection.commit()
            self._test_fault("AFTER_COMMIT")
        return StoredCheckpoint(checkpoint_id, generation, occurred_at, checkpoint_payload, checkpoint_digest)

    @staticmethod
    def utc_now() -> datetime:
        return datetime.now(timezone.utc)
