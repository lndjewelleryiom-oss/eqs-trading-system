from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Any


SOURCE_CLASS = "PUBLIC_UNADMITTED_EXPLORATORY"
ALLOWED_STATES = {"CREATED", "RUNNING", "FINISHED", "BLOCKED"}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(value: object) -> str:
    return sha256(_canonical(value)).hexdigest()


class FeasibilityTrialLedger:
    """Counted exploratory-trial ledger with an immutable hash-chained journal.

    This ledger is deliberately separate from the R1.3 Alpha evidence ledger. Nothing
    recorded here can create genuine-candidate or LIVE authority.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.db.close()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS feasibility_trial_meta(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            INSERT OR IGNORE INTO feasibility_trial_meta(key,value)
                VALUES('schema_id','EQS-FEASIBILITY-TRIAL-LEDGER-V2');
            CREATE TABLE IF NOT EXISTS feasibility_trials(
                trial_id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                campaign_fingerprint TEXT NOT NULL,
                source_class TEXT NOT NULL CHECK(source_class='PUBLIC_UNADMITTED_EXPLORATORY'),
                state TEXT NOT NULL CHECK(state IN ('CREATED','RUNNING','FINISHED','BLOCKED')),
                created_at TEXT NOT NULL,
                configuration_sha256 TEXT NOT NULL,
                acquisition_manifest_sha256 TEXT NOT NULL,
                result_sha256 TEXT,
                error_code TEXT,
                empirical_outcomes_consumed INTEGER NOT NULL CHECK(empirical_outcomes_consumed IN (0,1)),
                r13_authority INTEGER NOT NULL CHECK(r13_authority=0),
                live_authority INTEGER NOT NULL CHECK(live_authority=0)
            );
            CREATE TABLE IF NOT EXISTS feasibility_trial_results(
                trial_id TEXT PRIMARY KEY,
                result_json TEXT NOT NULL,
                result_sha256 TEXT NOT NULL,
                FOREIGN KEY(trial_id) REFERENCES feasibility_trials(trial_id)
            );
            CREATE TABLE IF NOT EXISTS feasibility_trial_journal(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                trial_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                prev_hash TEXT,
                journal_hash TEXT NOT NULL UNIQUE
            );
            CREATE TRIGGER IF NOT EXISTS feasibility_results_no_update
            BEFORE UPDATE ON feasibility_trial_results
            BEGIN SELECT RAISE(ABORT,'immutable feasibility result'); END;
            CREATE TRIGGER IF NOT EXISTS feasibility_results_no_delete
            BEFORE DELETE ON feasibility_trial_results
            BEGIN SELECT RAISE(ABORT,'immutable feasibility result'); END;
            CREATE TRIGGER IF NOT EXISTS feasibility_journal_no_update
            BEFORE UPDATE ON feasibility_trial_journal
            BEGIN SELECT RAISE(ABORT,'immutable feasibility journal'); END;
            CREATE TRIGGER IF NOT EXISTS feasibility_journal_no_delete
            BEFORE DELETE ON feasibility_trial_journal
            BEGIN SELECT RAISE(ABORT,'immutable feasibility journal'); END;
            """
        )
        self.db.commit()

    def _journal(self, trial_id: str, event_type: str, payload: dict[str, object]) -> None:
        previous = self.db.execute(
            "SELECT journal_hash FROM feasibility_trial_journal ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = None if previous is None else str(previous["journal_hash"])
        event_at = datetime.now(timezone.utc).isoformat()
        base = {
            "trial_id": trial_id,
            "event_type": event_type,
            "event_at": event_at,
            "payload": payload,
            "prev_hash": prev_hash,
        }
        self.db.execute(
            """INSERT INTO feasibility_trial_journal(
                trial_id,event_type,event_at,payload_json,prev_hash,journal_hash
            ) VALUES(?,?,?,?,?,?)""",
            (trial_id, event_type, event_at, _canonical(payload).decode("utf-8"), prev_hash, _sha(base)),
        )

    def register_trial(
        self,
        *,
        trial_id: str,
        campaign_id: str,
        campaign_fingerprint: str,
        configuration_sha256: str,
        acquisition_manifest_sha256: str,
    ) -> dict[str, Any]:
        if not trial_id.strip():
            raise ValueError("trial_id is required")
        existing = self.db.execute(
            "SELECT * FROM feasibility_trials WHERE trial_id=?", (trial_id,)
        ).fetchone()
        expected = (
            campaign_id,
            campaign_fingerprint,
            configuration_sha256,
            acquisition_manifest_sha256,
        )
        if existing is not None:
            actual = (
                str(existing["campaign_id"]),
                str(existing["campaign_fingerprint"]),
                str(existing["configuration_sha256"]),
                str(existing["acquisition_manifest_sha256"]),
            )
            if actual != expected:
                raise ValueError("conflicting reuse of feasibility trial_id")
            return dict(existing)
        now = datetime.now(timezone.utc).isoformat()
        self.db.execute(
            """INSERT INTO feasibility_trials(
                trial_id,campaign_id,campaign_fingerprint,source_class,state,created_at,
                configuration_sha256,acquisition_manifest_sha256,result_sha256,error_code,
                empirical_outcomes_consumed,r13_authority,live_authority
            ) VALUES(?,?,?,?,?,?,?,?,NULL,NULL,0,0,0)""",
            (
                trial_id, campaign_id, campaign_fingerprint, SOURCE_CLASS, "CREATED", now,
                configuration_sha256, acquisition_manifest_sha256,
            ),
        )
        self._journal(trial_id, "TRIAL_REGISTERED", {"state": "CREATED", "source_class": SOURCE_CLASS})
        self.db.commit()
        return self.trial(trial_id)

    def transition(self, trial_id: str, state: str, *, error_code: str | None = None, outcomes_consumed: bool | None = None) -> None:
        if state not in ALLOWED_STATES:
            raise ValueError("unsupported feasibility trial state")
        row = self.db.execute("SELECT state,empirical_outcomes_consumed FROM feasibility_trials WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None:
            raise KeyError(trial_id)
        current = str(row["state"])
        allowed = {
            "CREATED": {"RUNNING", "BLOCKED"},
            "RUNNING": {"FINISHED", "BLOCKED"},
            "FINISHED": set(),
            "BLOCKED": set(),
        }[current]
        if state not in allowed:
            raise ValueError(f"illegal feasibility trial transition {current}->{state}")
        consumed = int(row["empirical_outcomes_consumed"])
        if outcomes_consumed is True:
            consumed = 1
        elif outcomes_consumed is False and consumed:
            raise ValueError("empirical outcome consumption cannot be reversed")
        self.db.execute(
            "UPDATE feasibility_trials SET state=?,error_code=?,empirical_outcomes_consumed=? WHERE trial_id=?",
            (state, error_code, consumed, trial_id),
        )
        self._journal(trial_id, "STATE", {"state": state, "error_code": error_code, "empirical_outcomes_consumed": bool(consumed)})
        self.db.commit()

    def persist_result(self, trial_id: str, result: dict[str, object]) -> str:
        row = self.db.execute("SELECT state FROM feasibility_trials WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None:
            raise KeyError(trial_id)
        if str(row["state"]) != "RUNNING":
            raise ValueError("result can only be persisted for RUNNING trial")
        digest = _sha(result)
        encoded = _canonical(result).decode("utf-8")
        self.db.execute(
            "INSERT INTO feasibility_trial_results(trial_id,result_json,result_sha256) VALUES(?,?,?)",
            (trial_id, encoded, digest),
        )
        self.db.execute("UPDATE feasibility_trials SET result_sha256=? WHERE trial_id=?", (digest, trial_id))
        self._journal(trial_id, "RESULT", {"result_sha256": digest})
        self.db.commit()
        return digest

    def trial(self, trial_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM feasibility_trials WHERE trial_id=?", (trial_id,)).fetchone()
        if row is None:
            raise KeyError(trial_id)
        return dict(row)

    def trial_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM feasibility_trials").fetchone()[0])

    def verify_hash_chain(self) -> bool:
        previous: str | None = None
        for row in self.db.execute("SELECT * FROM feasibility_trial_journal ORDER BY seq"):
            payload = json.loads(str(row["payload_json"]))
            base = {
                "trial_id": str(row["trial_id"]),
                "event_type": str(row["event_type"]),
                "event_at": str(row["event_at"]),
                "payload": payload,
                "prev_hash": row["prev_hash"],
            }
            if row["prev_hash"] != previous or row["journal_hash"] != _sha(base):
                return False
            previous = str(row["journal_hash"])
        return True
