from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

from .attribution import PersistentPaperAttributionLedger
from .lifecycle import PersistentStrategyLifecycle, StrategyLifecycleState


def _canonical(obj: object) -> bytes:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    ).encode("utf-8")


def _sealed(path: Path, field: str = "record_sha256") -> tuple[dict[str, Any] | None, bool]:
    if not path.is_file():
        return None, False
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        expected = doc.get(field)
        if not isinstance(expected, str):
            return doc, False
        body = dict(doc)
        body.pop(field, None)
        return doc, sha256(_canonical(body)).hexdigest() == expected
    except Exception:
        return None, False


@dataclass(frozen=True, slots=True)
class ManagedPaperEvidence:
    status: str
    elapsed_hours: float
    total_attributed_trades: int
    genuine_strategy_count: int
    attribution_hash_chain_valid: bool
    broker_send_count: int
    live_authority: bool
    continuity_window_started_at: str | None
    last_observed_at: str
    blocker_codes: tuple[str, ...]


class ManagedPaperEvidenceRecorder:
    """Builds the sustained genuine PAPER evidence required for autonomy readiness."""

    QUALIFYING_STATES = {
        StrategyLifecycleState.PAPER_CANARY,
        StrategyLifecycleState.PAPER_ACTIVE,
        StrategyLifecycleState.REDUCED,
    }

    def __init__(
        self,
        root: str | Path,
        *,
        max_observation_gap_seconds: float = 300.0,
        required_hours: float = 168.0,
        required_attributed_trades: int = 100,
    ):
        self.root = Path(root)
        self.ev = self.root / "artifacts" / "test-evidence"
        self.runtime_db = (
            self.root / "artifacts" / "commissioning" / "current_runtime_v1" / "runtime.db"
        )
        self.lifecycle_db = self.ev / "EQS_STRATEGY_LIFECYCLE.db"
        self.attribution_db = self.ev / "EQS_PAPER_ATTRIBUTION_LEDGER.db"
        self.observation_db = self.ev / "EQS_MANAGED_PAPER_PERFORMANCE_LEDGER.db"
        self.output = self.ev / "EQS_MANAGED_PAPER_PERFORMANCE_EVIDENCE.json"
        self.max_observation_gap_seconds = float(max_observation_gap_seconds)
        self.required_hours = float(required_hours)
        self.required_attributed_trades = int(required_attributed_trades)
        if self.max_observation_gap_seconds <= 0:
            raise ValueError("max_observation_gap_seconds must be positive")
        if self.required_hours <= 0 or self.required_attributed_trades <= 0:
            raise ValueError("readiness thresholds must be positive")
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.observation_db, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self) -> None:
        self.ev.mkdir(parents=True, exist_ok=True)
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS managed_paper_observations(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    qualifying INTEGER NOT NULL CHECK(qualifying IN (0,1)),
                    genuine_strategy_count INTEGER NOT NULL,
                    total_eligible_attributed_fills INTEGER NOT NULL,
                    attribution_hash_chain_valid INTEGER NOT NULL CHECK(attribution_hash_chain_valid IN (0,1)),
                    supervisor_healthy INTEGER NOT NULL CHECK(supervisor_healthy IN (0,1)),
                    sustained_health_ok INTEGER NOT NULL CHECK(sustained_health_ok IN (0,1)),
                    broker_send_count INTEGER NOT NULL,
                    live_authority INTEGER NOT NULL CHECK(live_authority IN (0,1)),
                    reason_codes_json TEXT NOT NULL,
                    previous_hash TEXT,
                    observation_hash TEXT NOT NULL UNIQUE
                );
                CREATE TRIGGER IF NOT EXISTS managed_observations_no_update
                BEFORE UPDATE ON managed_paper_observations
                BEGIN SELECT RAISE(ABORT,'immutable managed PAPER observation'); END;
                CREATE TRIGGER IF NOT EXISTS managed_observations_no_delete
                BEFORE DELETE ON managed_paper_observations
                BEGIN SELECT RAISE(ABORT,'immutable managed PAPER observation'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def _broker_send_count(self) -> int:
        if not self.runtime_db.is_file():
            return 0
        con = sqlite3.connect(self.runtime_db, timeout=10)
        con.row_factory = sqlite3.Row
        count = 0
        try:
            rows = con.execute(
                "SELECT payload_json FROM runtime_events WHERE event_type='ORDER_EVALUATED'"
            ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                except Exception:
                    count += 1
                    continue
                if payload.get("sent") is True or payload.get("broker_submitted") is True:
                    count += 1
        finally:
            con.close()
        return count

    def _live_authority(self) -> bool:
        state, state_ok = _sealed(self.ev / "EQS00_PROGRAMME_STATE.json")
        policy, policy_ok = _sealed(self.ev / "EQS_LIVE_ADMISSION_POLICY.json")
        if not state_ok or not policy_ok:
            return True
        hard = state.get("hard_boundaries") or {}
        if hard.get("live_authority") is not False:
            return True
        if policy.get("live_authority_permitted") is not False:
            return True
        return False

    def _health(self) -> tuple[bool, bool]:
        supervisor, sup_ok = _sealed(self.ev / "EQS_SUPERVISOR_STATE.json")
        sustained, sus_ok = _sealed(self.ev / "EQS_SUSTAINED_PAPER_HEALTH_LATEST.json")
        supervisor_healthy = bool(
            sup_ok
            and supervisor.get("status") == "HEALTHY"
            and (supervisor.get("runtime") or {}).get("verified") is True
            and (supervisor.get("dashboard") or {}).get("verified") is True
        )
        sustained_healthy = bool(sus_ok and sustained.get("status") == "PASS")
        return supervisor_healthy, sustained_healthy

    def _genuine_managed_strategies(self) -> set[tuple[str, str]]:
        lifecycle = PersistentStrategyLifecycle(self.lifecycle_db)
        return {
            (record.strategy_id, record.strategy_version)
            for record in lifecycle.records()
            if record.source_class == "GENUINE" and record.state in self.QUALIFYING_STATES
        }

    def _attribution(self) -> tuple[PersistentPaperAttributionLedger, int, bool]:
        ledger = PersistentPaperAttributionLedger(self.attribution_db)
        if self.runtime_db.is_file():
            ledger.ingest_runtime_execution(self.runtime_db)
        fills = ledger.fills(eligible_only=True)
        return ledger, len(fills), ledger.verify_hash_chain()

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _current_window_start(self, con: sqlite3.Connection, now: datetime) -> str | None:
        rows = con.execute(
            """SELECT observed_at,qualifying FROM managed_paper_observations
               ORDER BY sequence DESC"""
        ).fetchall()
        if not rows or not bool(rows[0]["qualifying"]):
            return None
        later = now
        start = self._parse_time(rows[0]["observed_at"])
        for row in rows:
            at = self._parse_time(row["observed_at"])
            gap = (later - at).total_seconds()
            if not bool(row["qualifying"]) or gap > self.max_observation_gap_seconds:
                break
            start = at
            later = at
        return start.isoformat().replace("+00:00", "Z")

    def _trades_in_window(
        self,
        ledger: PersistentPaperAttributionLedger,
        managed: set[tuple[str, str]],
        start: str | None,
    ) -> int:
        if start is None or not managed:
            return 0
        start_dt = self._parse_time(start)
        count = 0
        for fill in ledger.fills(eligible_only=True):
            if (fill.strategy_id, fill.strategy_version) not in managed:
                continue
            if self._parse_time(fill.occurred_at) >= start_dt:
                count += 1
        return count

    def record(self, *, now: datetime | None = None) -> ManagedPaperEvidence:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        managed = self._genuine_managed_strategies()
        attribution, total_eligible_fills, attribution_ok = self._attribution()
        supervisor_ok, sustained_ok = self._health()
        broker_sends = self._broker_send_count()
        live = self._live_authority()

        reasons: list[str] = []
        if not managed:
            reasons.append("NO_GENUINE_MANAGED_PAPER_STRATEGY")
        if not attribution_ok:
            reasons.append("ATTRIBUTION_HASH_CHAIN_INVALID")
        if not supervisor_ok:
            reasons.append("SUPERVISOR_NOT_HEALTHY")
        if not sustained_ok:
            reasons.append("SUSTAINED_PAPER_HEALTH_NOT_PASS")
        if broker_sends != 0:
            reasons.append("BROKER_SEND_COUNT_NONZERO")
        if live:
            reasons.append("LIVE_AUTHORITY_NOT_FALSE")

        qualifying = not reasons
        con = self._connect()
        try:
            with con:
                prev = con.execute(
                    "SELECT observation_hash FROM managed_paper_observations ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                previous_hash = prev["observation_hash"] if prev else None
                observed_at = now.isoformat().replace("+00:00", "Z")
                body = {
                    "observed_at": observed_at,
                    "qualifying": qualifying,
                    "genuine_strategy_count": len(managed),
                    "total_eligible_attributed_fills": total_eligible_fills,
                    "attribution_hash_chain_valid": attribution_ok,
                    "supervisor_healthy": supervisor_ok,
                    "sustained_health_ok": sustained_ok,
                    "broker_send_count": broker_sends,
                    "live_authority": live,
                    "reason_codes": tuple(reasons),
                    "previous_hash": previous_hash,
                }
                observation_hash = sha256(_canonical(body)).hexdigest()
                con.execute(
                    """INSERT INTO managed_paper_observations(
                       observed_at,qualifying,genuine_strategy_count,
                       total_eligible_attributed_fills,attribution_hash_chain_valid,
                       supervisor_healthy,sustained_health_ok,broker_send_count,
                       live_authority,reason_codes_json,previous_hash,observation_hash
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        observed_at,
                        1 if qualifying else 0,
                        len(managed),
                        total_eligible_fills,
                        1 if attribution_ok else 0,
                        1 if supervisor_ok else 0,
                        1 if sustained_ok else 0,
                        broker_sends,
                        1 if live else 0,
                        json.dumps(tuple(reasons), sort_keys=True),
                        previous_hash,
                        observation_hash,
                    ),
                )

            window_start = self._current_window_start(con, now)
            elapsed_hours = (
                (now - self._parse_time(window_start)).total_seconds() / 3600.0
                if window_start is not None
                else 0.0
            )
            attributed_in_window = self._trades_in_window(attribution, managed, window_start)
        finally:
            con.close()

        status = (
            "PASS"
            if qualifying
            and elapsed_hours >= self.required_hours
            and attributed_in_window >= self.required_attributed_trades
            and len(managed) >= 1
            else "RUNNING" if qualifying else "BLOCKED"
        )

        record = {
            "schema_id": "EQS-MANAGED-PAPER-PERFORMANCE-EVIDENCE-V1",
            "created_at": now.isoformat().replace("+00:00", "Z"),
            "status": status,
            "elapsed_hours": elapsed_hours,
            "continuity_window_started_at": window_start,
            "maximum_observation_gap_seconds": self.max_observation_gap_seconds,
            "required_hours": self.required_hours,
            "total_attributed_trades": attributed_in_window,
            "required_attributed_trades": self.required_attributed_trades,
            "genuine_strategy_count": len(managed),
            "genuine_strategies": [
                {"strategy_id": sid, "strategy_version": version}
                for sid, version in sorted(managed)
            ],
            "total_eligible_attributed_fills_all_time": total_eligible_fills,
            "attribution_hash_chain_valid": attribution_ok,
            "supervisor_healthy": supervisor_ok,
            "sustained_health_ok": sustained_ok,
            "broker_send_count": broker_sends,
            "live_authority": live,
            "blockers": sorted(set(reasons)),
            "performance_autonomy_certified": False,
        }
        record["record_sha256"] = sha256(_canonical(record)).hexdigest()
        tmp = self.output.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        tmp.replace(self.output)

        return ManagedPaperEvidence(
            status=status,
            elapsed_hours=elapsed_hours,
            total_attributed_trades=attributed_in_window,
            genuine_strategy_count=len(managed),
            attribution_hash_chain_valid=attribution_ok,
            broker_send_count=broker_sends,
            live_authority=live,
            continuity_window_started_at=window_start,
            last_observed_at=record["created_at"],
            blocker_codes=tuple(record["blockers"]),
        )

    def verify_observation_hash_chain(self) -> bool:
        con = self._connect()
        try:
            previous = None
            for row in con.execute(
                "SELECT * FROM managed_paper_observations ORDER BY sequence"
            ):
                body = {
                    "observed_at": row["observed_at"],
                    "qualifying": bool(row["qualifying"]),
                    "genuine_strategy_count": int(row["genuine_strategy_count"]),
                    "total_eligible_attributed_fills": int(
                        row["total_eligible_attributed_fills"]
                    ),
                    "attribution_hash_chain_valid": bool(
                        row["attribution_hash_chain_valid"]
                    ),
                    "supervisor_healthy": bool(row["supervisor_healthy"]),
                    "sustained_health_ok": bool(row["sustained_health_ok"]),
                    "broker_send_count": int(row["broker_send_count"]),
                    "live_authority": bool(row["live_authority"]),
                    "reason_codes": tuple(json.loads(row["reason_codes_json"])),
                    "previous_hash": row["previous_hash"],
                }
                if (
                    row["previous_hash"] != previous
                    or row["observation_hash"] != sha256(_canonical(body)).hexdigest()
                ):
                    return False
                previous = row["observation_hash"]
            return True
        finally:
            con.close()
