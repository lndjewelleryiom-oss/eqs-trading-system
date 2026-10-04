from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import json
from pathlib import Path
import sqlite3


class StrategyLifecycleState(StrEnum):
    RESEARCH = "RESEARCH"
    VALIDATED = "VALIDATED"
    PAPER_CANARY = "PAPER_CANARY"
    PAPER_ACTIVE = "PAPER_ACTIVE"
    REDUCED = "REDUCED"
    PAUSED = "PAUSED"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class StrategyLifecycleRecord:
    strategy_id: str
    strategy_version: str
    state: StrategyLifecycleState
    source_class: str
    research_trial_id: str | None
    research_evidence_sha256: str | None
    research_manifest_sha256: str | None
    asset_class: str | None
    family: str | None
    horizon: str | None
    signal_description: str | None
    updated_at: str
    transition_generation: int


class StrategyLifecycleError(RuntimeError):
    pass


def _canonical(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class PersistentStrategyLifecycle:
    """Fail-closed PAPER strategy lifecycle.

    There is deliberately no LIVE state. Promotion beyond PAPER is owned by a
    separate programme authority gate and cannot be expressed here.
    """

    ALLOWED = {
        StrategyLifecycleState.RESEARCH: {
            StrategyLifecycleState.VALIDATED,
            StrategyLifecycleState.RETIRED,
        },
        StrategyLifecycleState.VALIDATED: {
            StrategyLifecycleState.PAPER_CANARY,
            StrategyLifecycleState.RETIRED,
        },
        StrategyLifecycleState.PAPER_CANARY: {
            StrategyLifecycleState.PAPER_ACTIVE,
            StrategyLifecycleState.REDUCED,
            StrategyLifecycleState.PAUSED,
            StrategyLifecycleState.RETIRED,
        },
        StrategyLifecycleState.PAPER_ACTIVE: {
            StrategyLifecycleState.REDUCED,
            StrategyLifecycleState.PAUSED,
            StrategyLifecycleState.RETIRED,
        },
        StrategyLifecycleState.REDUCED: {
            StrategyLifecycleState.PAPER_ACTIVE,
            StrategyLifecycleState.PAUSED,
            StrategyLifecycleState.RETIRED,
        },
        StrategyLifecycleState.PAUSED: {
            StrategyLifecycleState.PAPER_CANARY,
            StrategyLifecycleState.RETIRED,
        },
        StrategyLifecycleState.RETIRED: set(),
    }

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _init_schema(self) -> None:
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_lifecycle(
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    state TEXT NOT NULL,
                    source_class TEXT NOT NULL CHECK(source_class IN ('SYNTHETIC','GENUINE')),
                    research_trial_id TEXT,
                    research_evidence_sha256 TEXT,
                    research_manifest_sha256 TEXT,
                    asset_class TEXT,
                    family TEXT,
                    horizon TEXT,
                    signal_description TEXT,
                    updated_at TEXT NOT NULL,
                    transition_generation INTEGER NOT NULL,
                    PRIMARY KEY(strategy_id,strategy_version)
                );

                CREATE TABLE IF NOT EXISTS strategy_lifecycle_journal(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    from_state TEXT,
                    to_state TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    reason_codes_json TEXT NOT NULL,
                    evidence_sha256 TEXT,
                    transition_generation INTEGER NOT NULL,
                    previous_hash TEXT,
                    journal_hash TEXT NOT NULL UNIQUE
                );

                CREATE TRIGGER IF NOT EXISTS lifecycle_journal_no_update
                BEFORE UPDATE ON strategy_lifecycle_journal
                BEGIN SELECT RAISE(ABORT,'immutable lifecycle journal'); END;
                CREATE TRIGGER IF NOT EXISTS lifecycle_journal_no_delete
                BEFORE DELETE ON strategy_lifecycle_journal
                BEGIN SELECT RAISE(ABORT,'immutable lifecycle journal'); END;
                """
            )
            existing = {row[1] for row in con.execute("PRAGMA table_info(strategy_lifecycle)")}
            for name in ("asset_class", "family", "horizon", "signal_description"):
                if name not in existing:
                    con.execute(f"ALTER TABLE strategy_lifecycle ADD COLUMN {name} TEXT")
            con.commit()
        finally:
            con.close()

    def _append_journal(
        self,
        con: sqlite3.Connection,
        *,
        strategy_id: str,
        strategy_version: str,
        from_state: str | None,
        to_state: str,
        reasons: tuple[str, ...],
        evidence_sha256: str | None,
        generation: int,
    ) -> None:
        prev = con.execute(
            "SELECT journal_hash FROM strategy_lifecycle_journal ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = prev["journal_hash"] if prev else None
        occurred_at = datetime.now(timezone.utc).isoformat()
        body = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "from_state": from_state,
            "to_state": to_state,
            "occurred_at": occurred_at,
            "reason_codes": reasons,
            "evidence_sha256": evidence_sha256,
            "transition_generation": generation,
            "previous_hash": previous_hash,
        }
        journal_hash = sha256(_canonical(body)).hexdigest()
        con.execute(
            """INSERT INTO strategy_lifecycle_journal(
               strategy_id,strategy_version,from_state,to_state,occurred_at,
               reason_codes_json,evidence_sha256,transition_generation,
               previous_hash,journal_hash
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                strategy_id,
                strategy_version,
                from_state,
                to_state,
                occurred_at,
                json.dumps(reasons, sort_keys=True),
                evidence_sha256,
                generation,
                previous_hash,
                journal_hash,
            ),
        )

    def register(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        source_class: str,
        research_trial_id: str | None = None,
        research_evidence_sha256: str | None = None,
        research_manifest_sha256: str | None = None,
        asset_class: str | None = None,
        family: str | None = None,
        horizon: str | None = None,
        signal_description: str | None = None,
        reasons: tuple[str, ...] = ("REGISTERED_FOR_RESEARCH",),
    ) -> StrategyLifecycleRecord:
        if not strategy_id.strip() or not strategy_version.strip():
            raise StrategyLifecycleError("strategy identity must not be empty")
        if source_class not in {"SYNTHETIC", "GENUINE"}:
            raise StrategyLifecycleError("source_class must be SYNTHETIC or GENUINE")
        con = self._connect()
        try:
            with con:
                existing = con.execute(
                    "SELECT 1 FROM strategy_lifecycle WHERE strategy_id=? AND strategy_version=?",
                    (strategy_id, strategy_version),
                ).fetchone()
                if existing:
                    raise StrategyLifecycleError("strategy already registered")
                now = datetime.now(timezone.utc).isoformat()
                con.execute(
                    """INSERT INTO strategy_lifecycle(
                       strategy_id,strategy_version,state,source_class,research_trial_id,
                       research_evidence_sha256,research_manifest_sha256,asset_class,family,horizon,
                       signal_description,updated_at,transition_generation
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        strategy_id,
                        strategy_version,
                        StrategyLifecycleState.RESEARCH.value,
                        source_class,
                        research_trial_id,
                        research_evidence_sha256,
                        research_manifest_sha256,
                        asset_class,
                        family,
                        horizon,
                        signal_description,
                        now,
                        1,
                    ),
                )
                self._append_journal(
                    con,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    from_state=None,
                    to_state=StrategyLifecycleState.RESEARCH.value,
                    reasons=reasons,
                    evidence_sha256=research_evidence_sha256,
                    generation=1,
                )
            return self.get(strategy_id, strategy_version)
        finally:
            con.close()

    def get(self, strategy_id: str, strategy_version: str) -> StrategyLifecycleRecord:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT * FROM strategy_lifecycle WHERE strategy_id=? AND strategy_version=?",
                (strategy_id, strategy_version),
            ).fetchone()
            if row is None:
                raise StrategyLifecycleError("unknown strategy")
            return StrategyLifecycleRecord(
                strategy_id=row["strategy_id"],
                strategy_version=row["strategy_version"],
                state=StrategyLifecycleState(row["state"]),
                source_class=row["source_class"],
                research_trial_id=row["research_trial_id"],
                research_evidence_sha256=row["research_evidence_sha256"],
                research_manifest_sha256=row["research_manifest_sha256"],
                asset_class=row["asset_class"],
                family=row["family"],
                horizon=row["horizon"],
                signal_description=row["signal_description"],
                updated_at=row["updated_at"],
                transition_generation=int(row["transition_generation"]),
            )
        finally:
            con.close()

    def records(self) -> tuple[StrategyLifecycleRecord, ...]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT strategy_id,strategy_version FROM strategy_lifecycle ORDER BY strategy_id,strategy_version"
            ).fetchall()
        finally:
            con.close()
        return tuple(self.get(row["strategy_id"], row["strategy_version"]) for row in rows)

    def transition(
        self,
        strategy_id: str,
        strategy_version: str,
        target: StrategyLifecycleState,
        *,
        reasons: tuple[str, ...],
        evidence_sha256: str | None = None,
        genuine_research_gate_passed: bool = False,
    ) -> StrategyLifecycleRecord:
        if not reasons:
            raise StrategyLifecycleError("transition requires reason codes")
        current = self.get(strategy_id, strategy_version)
        if target not in self.ALLOWED[current.state]:
            raise StrategyLifecycleError(f"illegal transition {current.state.value}->{target.value}")

        if target == StrategyLifecycleState.VALIDATED:
            if current.source_class != "GENUINE":
                raise StrategyLifecycleError("SYNTHETIC_RESEARCH_CANNOT_VALIDATE")
            if not genuine_research_gate_passed:
                raise StrategyLifecycleError("GENUINE_RESEARCH_GATE_NOT_PASSED")
            if not current.research_trial_id or not current.research_evidence_sha256:
                raise StrategyLifecycleError("GENUINE_RESEARCH_EVIDENCE_MISSING")
            evidence_sha256 = evidence_sha256 or current.research_evidence_sha256

        if target == StrategyLifecycleState.PAPER_CANARY:
            if current.source_class != "GENUINE":
                raise StrategyLifecycleError("PAPER_REQUIRES_GENUINE_SOURCE")
            if current.state == StrategyLifecycleState.PAUSED and not evidence_sha256:
                raise StrategyLifecycleError("PAUSED_REENTRY_REQUIRES_REVALIDATION_EVIDENCE")

        con = self._connect()
        try:
            with con:
                row = con.execute(
                    """SELECT state,transition_generation FROM strategy_lifecycle
                       WHERE strategy_id=? AND strategy_version=?""",
                    (strategy_id, strategy_version),
                ).fetchone()
                if row is None:
                    raise StrategyLifecycleError("unknown strategy")
                if row["state"] != current.state.value or int(row["transition_generation"]) != current.transition_generation:
                    raise StrategyLifecycleError("LIFECYCLE_GENERATION_CONFLICT")
                generation = current.transition_generation + 1
                now = datetime.now(timezone.utc).isoformat()
                con.execute(
                    """UPDATE strategy_lifecycle SET state=?,updated_at=?,transition_generation=?
                       WHERE strategy_id=? AND strategy_version=?""",
                    (target.value, now, generation, strategy_id, strategy_version),
                )
                self._append_journal(
                    con,
                    strategy_id=strategy_id,
                    strategy_version=strategy_version,
                    from_state=current.state.value,
                    to_state=target.value,
                    reasons=tuple(reasons),
                    evidence_sha256=evidence_sha256,
                    generation=generation,
                )
            return self.get(strategy_id, strategy_version)
        finally:
            con.close()

    def verify_hash_chain(self) -> bool:
        con = self._connect()
        try:
            previous = None
            for row in con.execute("SELECT * FROM strategy_lifecycle_journal ORDER BY sequence"):
                reasons = tuple(json.loads(row["reason_codes_json"]))
                body = {
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "from_state": row["from_state"],
                    "to_state": row["to_state"],
                    "occurred_at": row["occurred_at"],
                    "reason_codes": reasons,
                    "evidence_sha256": row["evidence_sha256"],
                    "transition_generation": int(row["transition_generation"]),
                    "previous_hash": row["previous_hash"],
                }
                if row["previous_hash"] != previous or row["journal_hash"] != sha256(_canonical(body)).hexdigest():
                    return False
                previous = row["journal_hash"]
            return True
        finally:
            con.close()
