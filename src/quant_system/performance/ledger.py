from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any

INFRASTRUCTURE_TEST_STRATEGY_VERSION = "deterministic-infrastructure-test-v1"


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _payload_hash(payload: dict[str, Any]) -> str:
    return sha256(_canonical(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyActivity:
    strategy_id: str
    strategy_version: str
    runtime_id: str
    first_seen_at: str
    last_seen_at: str
    decision_count: int
    performance_claim_eligible: bool
    exclusion_reason: str | None


@dataclass(frozen=True, slots=True)
class PerformanceSnapshot:
    runtime_id: str
    first_observed_at: str
    last_observed_at: str
    observations: int
    starting_equity: Decimal
    ending_equity: Decimal
    return_fraction: Decimal
    max_drawdown_fraction: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    commissions: Decimal
    financing_costs: Decimal
    performance_claim_eligible: bool
    exclusion_reasons: tuple[str, ...]


class PerformanceLedger:
    """Immutable projection of PAPER runtime events into performance evidence.

    Operational PAPER valuations are preserved even when they are not valid
    strategy-performance evidence. Infrastructure-test strategies are ingested
    for lineage but are never eligible to support performance claims.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS ingest_state(
                    source_db TEXT PRIMARY KEY,
                    last_event_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS paper_valuations(
                    source_db TEXT NOT NULL,
                    source_event_id INTEGER NOT NULL,
                    runtime_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    cash TEXT NOT NULL,
                    equity TEXT NOT NULL,
                    realized_pnl TEXT NOT NULL,
                    unrealized_pnl TEXT NOT NULL,
                    commissions TEXT NOT NULL,
                    financing_costs TEXT NOT NULL,
                    source_classification TEXT,
                    source_venue TEXT,
                    source_payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY(source_db, source_event_id)
                );

                CREATE TABLE IF NOT EXISTS strategy_activity(
                    source_db TEXT NOT NULL,
                    source_event_id INTEGER NOT NULL,
                    runtime_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    signal TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    performance_claim_eligible INTEGER NOT NULL CHECK(performance_claim_eligible IN (0,1)),
                    exclusion_reason TEXT,
                    source_payload_sha256 TEXT NOT NULL,
                    PRIMARY KEY(source_db, source_event_id)
                );

                CREATE TABLE IF NOT EXISTS performance_journal(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_kind TEXT NOT NULL,
                    source_db TEXT NOT NULL,
                    source_event_id INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    previous_hash TEXT,
                    journal_hash TEXT NOT NULL UNIQUE
                );

                CREATE TRIGGER IF NOT EXISTS paper_valuations_no_update
                BEFORE UPDATE ON paper_valuations BEGIN SELECT RAISE(ABORT,'immutable performance valuation'); END;
                CREATE TRIGGER IF NOT EXISTS paper_valuations_no_delete
                BEFORE DELETE ON paper_valuations BEGIN SELECT RAISE(ABORT,'immutable performance valuation'); END;
                CREATE TRIGGER IF NOT EXISTS strategy_activity_no_update
                BEFORE UPDATE ON strategy_activity BEGIN SELECT RAISE(ABORT,'immutable strategy activity'); END;
                CREATE TRIGGER IF NOT EXISTS strategy_activity_no_delete
                BEFORE DELETE ON strategy_activity BEGIN SELECT RAISE(ABORT,'immutable strategy activity'); END;
                CREATE TRIGGER IF NOT EXISTS performance_journal_no_update
                BEFORE UPDATE ON performance_journal BEGIN SELECT RAISE(ABORT,'immutable performance journal'); END;
                CREATE TRIGGER IF NOT EXISTS performance_journal_no_delete
                BEFORE DELETE ON performance_journal BEGIN SELECT RAISE(ABORT,'immutable performance journal'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def _journal(
        self,
        con: sqlite3.Connection,
        *,
        event_kind: str,
        source_db: str,
        source_event_id: int,
        payload: dict[str, Any],
    ) -> None:
        row = con.execute(
            "SELECT journal_hash FROM performance_journal ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = row["journal_hash"] if row else None
        body = {
            "event_kind": event_kind,
            "source_db": source_db,
            "source_event_id": source_event_id,
            "payload": payload,
            "previous_hash": previous_hash,
        }
        con.execute(
            """INSERT INTO performance_journal(
               event_kind,source_db,source_event_id,payload_json,previous_hash,journal_hash
               ) VALUES(?,?,?,?,?,?)""",
            (
                event_kind,
                source_db,
                source_event_id,
                _canonical(payload),
                previous_hash,
                _payload_hash(body),
            ),
        )

    @staticmethod
    def _strategy_eligibility(payload: dict[str, Any]) -> tuple[bool, str | None]:
        version = str(payload.get("strategy_version", ""))
        reasons = tuple(str(x) for x in payload.get("reason_codes", []))
        if version == INFRASTRUCTURE_TEST_STRATEGY_VERSION:
            return False, "INFRASTRUCTURE_TEST_STRATEGY"
        if any(reason in {"DETERMINISTIC_TEST_SIGNAL_ONLY", "TEST_FEATURE_UNAVAILABLE"} for reason in reasons):
            return False, "TEST_SIGNAL_REASON_CODE"
        if not str(payload.get("strategy_id", "")).strip():
            return False, "STRATEGY_ID_MISSING"
        return True, None

    def ingest_runtime_events(self, runtime_db: str | Path) -> dict[str, int]:
        runtime_db = Path(runtime_db).resolve()
        source_db = str(runtime_db)
        src = sqlite3.connect(runtime_db, timeout=10.0)
        src.row_factory = sqlite3.Row
        dst = self._connect()
        counters = {"events_seen": 0, "valuations_added": 0, "strategy_events_added": 0}
        try:
            state = dst.execute(
                "SELECT last_event_id FROM ingest_state WHERE source_db=?", (source_db,)
            ).fetchone()
            last_id = int(state["last_event_id"]) if state else 0
            rows = src.execute(
                """SELECT id,runtime_id,event_type,occurred_at,payload_json,payload_sha256
                   FROM runtime_events WHERE id>? ORDER BY id""",
                (last_id,),
            ).fetchall()
            highest = last_id
            with dst:
                for row in rows:
                    counters["events_seen"] += 1
                    highest = max(highest, int(row["id"]))
                    payload = json.loads(row["payload_json"])
                    if _payload_hash(payload) != row["payload_sha256"]:
                        raise ValueError(f"runtime payload hash mismatch event_id={row['id']}")

                    if row["event_type"] == "PAPER_VALUATION" and row["runtime_id"].endswith("-paper"):
                        dst.execute(
                            """INSERT OR IGNORE INTO paper_valuations(
                               source_db,source_event_id,runtime_id,occurred_at,observed_at,cash,equity,
                               realized_pnl,unrealized_pnl,commissions,financing_costs,
                               source_classification,source_venue,source_payload_sha256
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                source_db,
                                int(row["id"]),
                                row["runtime_id"],
                                row["occurred_at"],
                                str(payload.get("observed_at", row["occurred_at"])),
                                str(payload.get("cash", "0")),
                                str(payload.get("equity", "0")),
                                str(payload.get("realized_pnl", "0")),
                                str(payload.get("unrealized_pnl", "0")),
                                str(payload.get("commissions", "0")),
                                str(payload.get("financing_costs", "0")),
                                str((payload.get("source") or {}).get("classification") or ""),
                                str((payload.get("source") or {}).get("venue") or ""),
                                row["payload_sha256"],
                            ),
                        )
                        if dst.execute("SELECT changes()").fetchone()[0]:
                            counters["valuations_added"] += 1
                            self._journal(
                                dst,
                                event_kind="PAPER_VALUATION_INGESTED",
                                source_db=source_db,
                                source_event_id=int(row["id"]),
                                payload={
                                    "runtime_id": row["runtime_id"],
                                    "equity": str(payload.get("equity", "0")),
                                    "occurred_at": row["occurred_at"],
                                    "source_payload_sha256": row["payload_sha256"],
                                },
                            )

                    elif row["event_type"] == "STRATEGY_DECISION" and row["runtime_id"].endswith("-paper"):
                        eligible, exclusion = self._strategy_eligibility(payload)
                        dst.execute(
                            """INSERT OR IGNORE INTO strategy_activity(
                               source_db,source_event_id,runtime_id,occurred_at,strategy_id,
                               strategy_version,signal,reason_codes_json,performance_claim_eligible,
                               exclusion_reason,source_payload_sha256
                               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                source_db,
                                int(row["id"]),
                                row["runtime_id"],
                                row["occurred_at"],
                                str(payload.get("strategy_id", "")),
                                str(payload.get("strategy_version", "")),
                                str(payload.get("signal", "")),
                                json.dumps(payload.get("reason_codes", []), sort_keys=True),
                                1 if eligible else 0,
                                exclusion,
                                row["payload_sha256"],
                            ),
                        )
                        if dst.execute("SELECT changes()").fetchone()[0]:
                            counters["strategy_events_added"] += 1
                            self._journal(
                                dst,
                                event_kind="STRATEGY_ACTIVITY_INGESTED",
                                source_db=source_db,
                                source_event_id=int(row["id"]),
                                payload={
                                    "runtime_id": row["runtime_id"],
                                    "strategy_id": str(payload.get("strategy_id", "")),
                                    "strategy_version": str(payload.get("strategy_version", "")),
                                    "eligible": eligible,
                                    "exclusion_reason": exclusion,
                                    "source_payload_sha256": row["payload_sha256"],
                                },
                            )

                dst.execute(
                    """INSERT INTO ingest_state(source_db,last_event_id,updated_at)
                       VALUES(?,?,?)
                       ON CONFLICT(source_db) DO UPDATE SET
                       last_event_id=excluded.last_event_id,
                       updated_at=excluded.updated_at""",
                    (source_db, highest, datetime.now(timezone.utc).isoformat()),
                )
        finally:
            src.close()
            dst.close()
        return counters

    def strategy_activity(self) -> tuple[StrategyActivity, ...]:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT strategy_id,strategy_version,runtime_id,
                          MIN(occurred_at) first_seen_at,MAX(occurred_at) last_seen_at,
                          COUNT(*) decision_count,MIN(performance_claim_eligible) performance_claim_eligible,
                          GROUP_CONCAT(DISTINCT exclusion_reason) exclusion_reasons
                   FROM strategy_activity
                   GROUP BY strategy_id,strategy_version,runtime_id
                   ORDER BY strategy_id,strategy_version,runtime_id"""
            ).fetchall()
            return tuple(
                StrategyActivity(
                    strategy_id=row["strategy_id"],
                    strategy_version=row["strategy_version"],
                    runtime_id=row["runtime_id"],
                    first_seen_at=row["first_seen_at"],
                    last_seen_at=row["last_seen_at"],
                    decision_count=int(row["decision_count"]),
                    performance_claim_eligible=bool(row["performance_claim_eligible"]),
                    exclusion_reason=row["exclusion_reasons"] or None,
                )
                for row in rows
            )
        finally:
            con.close()

    def runtime_snapshot(self, runtime_id: str) -> PerformanceSnapshot:
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT * FROM paper_valuations
                   WHERE runtime_id=? ORDER BY occurred_at,source_event_id""",
                (runtime_id,),
            ).fetchall()
            if not rows:
                raise ValueError(f"no PAPER valuations for runtime {runtime_id}")
            equities = [Decimal(row["equity"]) for row in rows]
            peak = equities[0]
            max_dd = Decimal("0")
            for equity in equities:
                if equity > peak:
                    peak = equity
                if peak > 0:
                    dd = (peak - equity) / peak
                    if dd > max_dd:
                        max_dd = dd
            start = equities[0]
            end = equities[-1]
            ret = (end / start - Decimal("1")) if start != 0 else Decimal("0")
            eligible_rows = con.execute(
                """SELECT COUNT(*) n FROM strategy_activity
                   WHERE runtime_id=? AND performance_claim_eligible=1""",
                (runtime_id,),
            ).fetchone()["n"]
            excluded = con.execute(
                """SELECT DISTINCT exclusion_reason FROM strategy_activity
                   WHERE runtime_id=? AND performance_claim_eligible=0
                   AND exclusion_reason IS NOT NULL ORDER BY exclusion_reason""",
                (runtime_id,),
            ).fetchall()
            reasons = tuple(row["exclusion_reason"] for row in excluded)
            if not eligible_rows:
                reasons = tuple(sorted(set(reasons + ("NO_ELIGIBLE_STRATEGY_ACTIVITY",))))
            last = rows[-1]
            return PerformanceSnapshot(
                runtime_id=runtime_id,
                first_observed_at=rows[0]["observed_at"],
                last_observed_at=last["observed_at"],
                observations=len(rows),
                starting_equity=start,
                ending_equity=end,
                return_fraction=ret,
                max_drawdown_fraction=max_dd,
                realized_pnl=Decimal(last["realized_pnl"]),
                unrealized_pnl=Decimal(last["unrealized_pnl"]),
                commissions=Decimal(last["commissions"]),
                financing_costs=Decimal(last["financing_costs"]),
                performance_claim_eligible=bool(eligible_rows),
                exclusion_reasons=reasons,
            )
        finally:
            con.close()

    def equity_series(self, runtime_id: str, *, limit: int = 1000) -> tuple[dict[str, str], ...]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        con = self._connect()
        try:
            rows = con.execute(
                """SELECT observed_at,equity,realized_pnl,unrealized_pnl,commissions,financing_costs
                   FROM paper_valuations WHERE runtime_id=?
                   ORDER BY occurred_at DESC,source_event_id DESC LIMIT ?""",
                (runtime_id, int(limit)),
            ).fetchall()
            return tuple(
                {
                    "observed_at": row["observed_at"],
                    "equity": row["equity"],
                    "realized_pnl": row["realized_pnl"],
                    "unrealized_pnl": row["unrealized_pnl"],
                    "commissions": row["commissions"],
                    "financing_costs": row["financing_costs"],
                }
                for row in reversed(rows)
            )
        finally:
            con.close()

    def verify_hash_chain(self) -> bool:
        con = self._connect()
        try:
            previous = None
            for row in con.execute("SELECT * FROM performance_journal ORDER BY sequence"):
                payload = json.loads(row["payload_json"])
                body = {
                    "event_kind": row["event_kind"],
                    "source_db": row["source_db"],
                    "source_event_id": int(row["source_event_id"]),
                    "payload": payload,
                    "previous_hash": row["previous_hash"],
                }
                if row["previous_hash"] != previous or row["journal_hash"] != _payload_hash(body):
                    return False
                previous = row["journal_hash"]
            return True
        finally:
            con.close()
