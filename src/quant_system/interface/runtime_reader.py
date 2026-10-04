from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import time
from typing import Any, Iterator


class RuntimeReadError(RuntimeError):
    """Raised when persisted runtime state cannot be trusted for read-only display."""


class RuntimeStoreReadOnlyReader:
    """Observe an F5.6 runtime SQLite store without creating or mutating it.

    The connection is opened with SQLite ``mode=ro`` and ``PRAGMA query_only=ON``.
    No runtime lease is claimed, renewed or released by this reader.
    """

    REQUIRED_TABLES = {"runtime_state", "runtime_events", "runtime_checkpoints"}

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise RuntimeReadError("configured runtime database does not exist")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        except sqlite3.Error as exc:
            raise RuntimeReadError(f"cannot open runtime database read-only: {type(exc).__name__}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            deadline = time.monotonic() + 1.0
            connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
            connection.execute("BEGIN")
            yield connection
        except (sqlite3.Error, ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeReadError(f"runtime schema/read incompatible: {type(exc).__name__}") from exc
        finally:
            connection.close()

    @staticmethod
    def _parse_time(value: str | None) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RuntimeReadError("persisted runtime timestamp is not timezone-aware")
        return parsed

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    @staticmethod
    def _verified_payload(row: sqlite3.Row, *, kind: str) -> dict[str, Any]:
        raw = str(row["payload_json"])
        if len(raw) > 4_000_000:
            raise RuntimeReadError("persisted payload exceeds read limit")
        expected = str(row["payload_sha256"])
        actual = sha256(raw.encode("utf-8")).hexdigest()
        if actual != expected:
            raise RuntimeReadError(f"{kind} payload hash verification failed")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeReadError(f"{kind} payload JSON is invalid") from exc
        if not isinstance(payload, dict):
            raise RuntimeReadError(f"{kind} payload must be a JSON object")
        return payload

    def snapshot(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._connection() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            missing = self.REQUIRED_TABLES - tables
            if missing:
                raise RuntimeReadError("runtime database schema missing: " + ",".join(sorted(missing)))
            state_rows = connection.execute(
                "SELECT * FROM runtime_state ORDER BY updated_at DESC, runtime_id LIMIT 65"
            ).fetchall()
            if len(state_rows) > 64:
                raise RuntimeReadError("runtime read limit exceeded: 64 runtimes")
            runtimes = [self._runtime_snapshot(connection, row, current) for row in state_rows]
        return {
            "source": "F5.6_PERSISTENT_RUNTIME_STORE",
            "read_only": True,
            "runtime_count": len(runtimes),
            "runtimes": runtimes,
        }

    def _runtime_snapshot(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        now: datetime,
    ) -> dict[str, Any]:
        runtime_id = str(row["runtime_id"])
        lease_expires_at = self._parse_time(row["lease_expires_at"])
        owner_present = row["owner_id"] is not None
        if not owner_present or lease_expires_at is None:
            lease_state = "NONE"
        elif lease_expires_at > now:
            lease_state = "ACTIVE"
        else:
            lease_state = "EXPIRED"

        events = connection.execute(
            "SELECT sequence,event_type,occurred_at,payload_json,payload_sha256 "
            "FROM runtime_events WHERE runtime_id=? ORDER BY sequence LIMIT 10001",
            (runtime_id,),
        ).fetchall()
        if len(events) > 10000:
            raise RuntimeReadError("event verification limit exceeded: dedicated summary service required")
        event_rows: list[dict[str, Any]] = []
        expected_sequence = 1
        for event in events:
            sequence = int(event["sequence"])
            if sequence != expected_sequence:
                raise RuntimeReadError(f"runtime event sequence gap for {runtime_id}")
            expected_sequence += 1
            payload = self._verified_payload(event, kind="runtime event")
            event_rows.append({
                "sequence": sequence,
                "event_type": str(event["event_type"]),
                "occurred_at": self._iso(self._parse_time(str(event["occurred_at"]))),
                "payload": payload,
            })

        head_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runtime_checkpoint_heads'"
        ).fetchone() is not None
        checkpoint_row = None
        if head_exists:
            checkpoint_row = connection.execute(
                "SELECT * FROM runtime_checkpoint_heads WHERE runtime_id=?",
                (runtime_id,),
            ).fetchone()
        if checkpoint_row is None:
            checkpoint_row = connection.execute(
                "SELECT * FROM runtime_checkpoints WHERE runtime_id=? ORDER BY id DESC LIMIT 1",
                (runtime_id,),
            ).fetchone()
        checkpoint: dict[str, Any] | None = None
        if checkpoint_row is not None:
            payload = self._verified_payload(checkpoint_row, kind="runtime checkpoint")
            if payload.get("runtime_id") != runtime_id or payload.get("mode") != row["mode"]:
                raise RuntimeReadError(f"runtime checkpoint identity mismatch for {runtime_id}")
            if head_exists and "archive_checkpoint_id" in checkpoint_row.keys() and checkpoint_row["archive_checkpoint_id"] is not None:
                archive = connection.execute(
                    "SELECT * FROM runtime_checkpoints WHERE id=? AND runtime_id=?",
                    (int(checkpoint_row["archive_checkpoint_id"]), runtime_id),
                ).fetchone()
                if archive is None:
                    raise RuntimeReadError(f"runtime checkpoint head archive reference missing for {runtime_id}")
                archive_payload = self._verified_payload(archive, kind="archived runtime checkpoint")
                if str(archive["payload_sha256"]) != str(checkpoint_row["payload_sha256"]) or archive_payload != payload:
                    raise RuntimeReadError(f"runtime checkpoint head/archive mismatch for {runtime_id}")
            checkpoint = {
                "checkpoint_id": int(checkpoint_row["id"]),
                "generation": int(checkpoint_row["generation"]),
                "created_at": self._iso(self._parse_time(str(checkpoint_row["created_at"]))),
                "sha256": str(checkpoint_row["payload_sha256"]),
                "payload": payload,
            }

        def latest_event(*event_types: str) -> dict[str, Any] | None:
            allowed = set(event_types)
            for item in reversed(event_rows):
                if item["event_type"] in allowed:
                    return item
            return None

        return {
            "runtime_id": runtime_id,
            "mode": str(row["mode"]),
            "status": str(row["status"]),
            "halt_reason": row["halt_reason"],
            "generation": int(row["generation"]),
            "lease_state": lease_state,
            "lease_expires_at": self._iso(lease_expires_at),
            "owner_present": owner_present,
            "created_at": self._iso(self._parse_time(str(row["created_at"]))),
            "updated_at": self._iso(self._parse_time(str(row["updated_at"]))),
            "last_heartbeat_at": self._iso(self._parse_time(row["last_heartbeat_at"])),
            "event_count": len(event_rows),
            "recent_events": event_rows[-100:],
            "events_verified": True,
            "latest_event": None if not event_rows else {k: event_rows[-1][k] for k in ("sequence", "event_type", "occurred_at")},
            "latest_degradation": latest_event("DEGRADATION_ASSESSMENT"),
            "latest_reconciliation": latest_event("PAPER_BAR_PROCESSED", "SHADOW_RECONCILIATION", "RECONCILIATION"),
            "checkpoint": checkpoint,
        }
