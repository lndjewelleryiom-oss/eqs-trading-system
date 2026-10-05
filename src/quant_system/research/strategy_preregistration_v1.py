from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Mapping

_SHA = re.compile(r"^[a-f0-9]{64}$")


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _hash(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


@dataclass(frozen=True, slots=True)
class StrategyPreregistration:
    strategy_id: str
    strategy_version: str
    family: str
    asset_class: str
    hypothesis: str
    economic_rationale: str
    timeframe: str
    signal_definition: str
    entry_rules: tuple[str, ...]
    exit_rules: tuple[str, ...]
    risk_rules: tuple[str, ...]
    parameter_space: Mapping[str, object]
    data_scope: Mapping[str, object]
    acceptance_criteria: Mapping[str, object]
    rejection_criteria: tuple[str, ...]
    robustness_tests: tuple[str, ...]
    max_family_trials: int
    max_parameter_combinations: int
    random_seed: int
    research_policy_sha256: str
    eligibility_matrix_sha256: str
    empirical_status: str = "UNVIEWED"
    broker_submission_enabled: bool = False
    live_authority: bool = False

    def __post_init__(self) -> None:
        for name in (
            "strategy_id", "strategy_version", "family", "asset_class", "hypothesis",
            "economic_rationale", "timeframe", "signal_definition",
        ):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"{name} is required")
        if not self.entry_rules or not self.exit_rules or not self.risk_rules:
            raise ValueError("entry, exit and risk rules are required")
        if not self.parameter_space or not self.data_scope or not self.acceptance_criteria:
            raise ValueError("parameter space, data scope and acceptance criteria are required")
        if not self.rejection_criteria or not self.robustness_tests:
            raise ValueError("rejection criteria and robustness tests are required")
        if self.max_family_trials <= 0 or self.max_parameter_combinations <= 0:
            raise ValueError("research budgets must be positive")
        if not _SHA.fullmatch(self.research_policy_sha256) or not _SHA.fullmatch(self.eligibility_matrix_sha256):
            raise ValueError("policy and eligibility bindings must be SHA-256")
        if self.empirical_status != "UNVIEWED":
            raise ValueError("new preregistration must start UNVIEWED")
        if self.broker_submission_enabled or self.live_authority:
            raise ValueError("research preregistration cannot grant broker or LIVE authority")

    def payload(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def fingerprint(self) -> str:
        return _hash(self.payload())

    @property
    def lineage_id(self) -> str:
        return f"{self.strategy_id}:{self.strategy_version}"


class StrategyPreregistrationLedger:
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
                CREATE TABLE IF NOT EXISTS strategy_preregistrations(
                    lineage_id TEXT PRIMARY KEY,
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    family TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    UNIQUE(strategy_id,strategy_version)
                );
                CREATE TABLE IF NOT EXISTS strategy_trial_events(
                    event_id TEXT PRIMARY KEY,
                    lineage_id TEXT NOT NULL,
                    trial_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    parameter_combinations INTEGER NOT NULL,
                    event_at TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    UNIQUE(lineage_id,trial_id,event_type),
                    FOREIGN KEY(lineage_id) REFERENCES strategy_preregistrations(lineage_id)
                );
                CREATE TRIGGER IF NOT EXISTS prereg_no_update BEFORE UPDATE ON strategy_preregistrations
                BEGIN SELECT RAISE(ABORT,'immutable strategy preregistration'); END;
                CREATE TRIGGER IF NOT EXISTS prereg_no_delete BEFORE DELETE ON strategy_preregistrations
                BEGIN SELECT RAISE(ABORT,'immutable strategy preregistration'); END;
                CREATE TRIGGER IF NOT EXISTS trial_event_no_update BEFORE UPDATE ON strategy_trial_events
                BEGIN SELECT RAISE(ABORT,'immutable trial event'); END;
                CREATE TRIGGER IF NOT EXISTS trial_event_no_delete BEFORE DELETE ON strategy_trial_events
                BEGIN SELECT RAISE(ABORT,'immutable trial event'); END;
                """
            )
            con.commit()
        finally:
            con.close()

    def register(self, prereg: StrategyPreregistration) -> dict[str, Any]:
        payload_json = json.dumps(prereg.payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        con = self._connect()
        try:
            existing = con.execute(
                "SELECT * FROM strategy_preregistrations WHERE lineage_id=?", (prereg.lineage_id,)
            ).fetchone()
            if existing is not None:
                if existing["fingerprint"] != prereg.fingerprint:
                    raise ValueError("strategy lineage already preregistered with different frozen content")
                return dict(existing)
            with con:
                con.execute(
                    "INSERT INTO strategy_preregistrations VALUES(?,?,?,?,?,?,?,?)",
                    (prereg.lineage_id, prereg.strategy_id, prereg.strategy_version, prereg.family,
                     prereg.asset_class, prereg.fingerprint, payload_json, now),
                )
            return self.get(prereg.lineage_id)
        finally:
            con.close()

    def get(self, lineage_id: str) -> dict[str, Any]:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM strategy_preregistrations WHERE lineage_id=?", (lineage_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            raise KeyError(lineage_id)
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        return value

    def reserve_trial(self, lineage_id: str, trial_id: str, *, parameter_combinations: int) -> dict[str, Any]:
        if parameter_combinations <= 0:
            raise ValueError("parameter_combinations must be positive")
        prereg = self.get(lineage_id)
        payload = prereg["payload"]
        con = self._connect()
        try:
            prior = con.execute(
                "SELECT trial_id,parameter_combinations FROM strategy_trial_events WHERE lineage_id=? AND event_type='RESERVED'",
                (lineage_id,),
            ).fetchall()
            if any(row["trial_id"] == trial_id for row in prior):
                raise ValueError("trial already reserved")
            if len(prior) + 1 > int(payload["max_family_trials"]):
                raise RuntimeError("family trial budget exhausted")
            consumed = sum(int(row["parameter_combinations"]) for row in prior)
            if consumed + parameter_combinations > int(payload["max_parameter_combinations"]):
                raise RuntimeError("parameter-combination budget exhausted")
            event = self._append_event(con, lineage_id, trial_id, "RESERVED", parameter_combinations, {})
            con.commit()
            return event
        finally:
            con.close()

    def close_trial(self, lineage_id: str, trial_id: str, *, outcome: str, detail: Mapping[str, object] | None = None) -> dict[str, Any]:
        if outcome not in {"PASSED", "REJECTED", "FAILED", "ABANDONED", "BLOCKED"}:
            raise ValueError("unsupported trial outcome")
        con = self._connect()
        try:
            reserved = con.execute(
                "SELECT parameter_combinations FROM strategy_trial_events WHERE lineage_id=? AND trial_id=? AND event_type='RESERVED'",
                (lineage_id, trial_id),
            ).fetchone()
            if reserved is None:
                raise ValueError("trial must be reserved before closure")
            event = self._append_event(con, lineage_id, trial_id, outcome, int(reserved["parameter_combinations"]), detail or {})
            con.commit()
            return event
        finally:
            con.close()

    def _append_event(self, con: sqlite3.Connection, lineage_id: str, trial_id: str, event_type: str, parameter_combinations: int, detail: Mapping[str, object]) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        detail_json = json.dumps(dict(detail), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        event_id = _hash({"lineage_id": lineage_id, "trial_id": trial_id, "event_type": event_type, "parameter_combinations": parameter_combinations, "event_at": now, "detail": dict(detail)})
        try:
            con.execute(
                "INSERT INTO strategy_trial_events VALUES(?,?,?,?,?,?,?)",
                (event_id, lineage_id, trial_id, event_type, parameter_combinations, now, detail_json),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("duplicate trial event") from exc
        return {"event_id": event_id, "lineage_id": lineage_id, "trial_id": trial_id, "event_type": event_type, "parameter_combinations": parameter_combinations, "event_at": now, "detail": dict(detail)}

    def trial_counts(self, lineage_id: str) -> dict[str, int]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT parameter_combinations FROM strategy_trial_events WHERE lineage_id=? AND event_type='RESERVED'",
                (lineage_id,),
            ).fetchall()
        finally:
            con.close()
        return {"trials": len(rows), "parameter_combinations": sum(int(r["parameter_combinations"]) for r in rows)}
