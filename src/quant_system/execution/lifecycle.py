from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import sqlite3
from uuid import UUID
from quant_system.execution.adapter_contract import NormalizedExecutionState

_UNRESOLVED={NormalizedExecutionState.SUBMISSION_PENDING,NormalizedExecutionState.UNKNOWN,NormalizedExecutionState.RECONCILIATION_REQUIRED}
_TERMINAL={NormalizedExecutionState.FILLED,NormalizedExecutionState.REJECTED,NormalizedExecutionState.CANCELLED,NormalizedExecutionState.EXPIRED}

@dataclass(frozen=True,slots=True)
class DurableExecution:
    intent_id: UUID
    state: NormalizedExecutionState
    venue_order_id: str|None
    reason: str
    created_at: datetime
    updated_at: datetime

class DuplicateExecutionConflict(RuntimeError): pass
class ExecutionBlocked(RuntimeError): pass
class InvalidExecutionTransition(RuntimeError): pass

class DurableExecutionLedger:
    """Crash-safe execution identity/state ledger. Contains no venue submission code."""
    def __init__(self,path:str|Path):
        self.path=Path(path); self.path.parent.mkdir(parents=True,exist_ok=True); self._init()
    def _connect(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL");c.execute("PRAGMA synchronous=FULL");c.execute("PRAGMA busy_timeout=10000")
        return c
    def _init(self):
        with self._connect() as c:c.executescript("""CREATE TABLE IF NOT EXISTS execution_lifecycle(
        intent_id TEXT PRIMARY KEY,state TEXT NOT NULL,venue_order_id TEXT,reason TEXT NOT NULL,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS execution_transitions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,intent_id TEXT NOT NULL,state TEXT NOT NULL,
        reason TEXT NOT NULL,occurred_at TEXT NOT NULL,
        FOREIGN KEY(intent_id) REFERENCES execution_lifecycle(intent_id));
        CREATE INDEX IF NOT EXISTS idx_execution_state ON execution_lifecycle(state);""")
    @staticmethod
    def _row(r):
        if r is None:return None
        return DurableExecution(UUID(r["intent_id"]),NormalizedExecutionState(r["state"]),r["venue_order_id"],r["reason"],datetime.fromisoformat(r["created_at"]),datetime.fromisoformat(r["updated_at"]))
    def get(self,intent_id):
        with self._connect() as c:r=c.execute("SELECT * FROM execution_lifecycle WHERE intent_id=?",(str(intent_id),)).fetchone()
        return self._row(r)
    def reserve(self,intent_id,*,now):
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE");r=c.execute("SELECT * FROM execution_lifecycle WHERE intent_id=?",(str(intent_id),)).fetchone()
            if r is not None:return self._row(r)
            c.execute("INSERT INTO execution_lifecycle VALUES(?,?,?,?,?,?)",(str(intent_id),NormalizedExecutionState.INTENT_CREATED.value,None,"RESERVED",now.isoformat(),now.isoformat()))
            c.execute("INSERT INTO execution_transitions(intent_id,state,reason,occurred_at) VALUES(?,?,?,?)",(str(intent_id),NormalizedExecutionState.INTENT_CREATED.value,"RESERVED",now.isoformat()));c.commit()
        return self.get(intent_id)
    def transition(self,intent_id,state,*,now,reason,venue_order_id=None):
        current=self.get(intent_id)
        if current is None:raise KeyError(str(intent_id))
        allowed={
          NormalizedExecutionState.INTENT_CREATED:{NormalizedExecutionState.VALIDATED,NormalizedExecutionState.REJECTED},
          NormalizedExecutionState.VALIDATED:{NormalizedExecutionState.RISK_AUTHORISED,NormalizedExecutionState.REJECTED},
          NormalizedExecutionState.RISK_AUTHORISED:{NormalizedExecutionState.ROUTED,NormalizedExecutionState.REJECTED},
          NormalizedExecutionState.ROUTED:{NormalizedExecutionState.SUBMISSION_PENDING,NormalizedExecutionState.REJECTED},
          NormalizedExecutionState.SUBMISSION_PENDING:{NormalizedExecutionState.ACKNOWLEDGED,NormalizedExecutionState.REJECTED,NormalizedExecutionState.UNKNOWN},
          NormalizedExecutionState.ACKNOWLEDGED:{NormalizedExecutionState.PARTIALLY_FILLED,NormalizedExecutionState.FILLED,NormalizedExecutionState.CANCEL_PENDING,NormalizedExecutionState.UNKNOWN},
          NormalizedExecutionState.PARTIALLY_FILLED:{NormalizedExecutionState.PARTIALLY_FILLED,NormalizedExecutionState.FILLED,NormalizedExecutionState.CANCEL_PENDING,NormalizedExecutionState.UNKNOWN},
          NormalizedExecutionState.CANCEL_PENDING:{NormalizedExecutionState.CANCELLED,NormalizedExecutionState.UNKNOWN},
          NormalizedExecutionState.UNKNOWN:{NormalizedExecutionState.RECONCILIATION_REQUIRED},
          NormalizedExecutionState.RECONCILIATION_REQUIRED:{NormalizedExecutionState.ACKNOWLEDGED,NormalizedExecutionState.PARTIALLY_FILLED,NormalizedExecutionState.FILLED,NormalizedExecutionState.REJECTED,NormalizedExecutionState.CANCELLED},
        }
        if state not in allowed.get(current.state,set()):raise InvalidExecutionTransition(f"{current.state.value}->{state.value}")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE");db.execute("UPDATE execution_lifecycle SET state=?,venue_order_id=COALESCE(?,venue_order_id),reason=?,updated_at=? WHERE intent_id=?",(state.value,venue_order_id,reason,now.isoformat(),str(intent_id)))
            db.execute("INSERT INTO execution_transitions(intent_id,state,reason,occurred_at) VALUES(?,?,?,?)",(str(intent_id),state.value,reason,now.isoformat()));db.commit()
        return self.get(intent_id)
    def recover_after_restart(self,*,now):
        with self._connect() as db:
            rows=db.execute("SELECT intent_id FROM execution_lifecycle WHERE state=?",(NormalizedExecutionState.SUBMISSION_PENDING.value,)).fetchall()
        recovered=[]
        for row in rows:
            iid=UUID(row["intent_id"]);self.transition(iid,NormalizedExecutionState.UNKNOWN,now=now,reason="UNCLEAN_RESTART_DURING_SUBMISSION")
            recovered.append(self.transition(iid,NormalizedExecutionState.RECONCILIATION_REQUIRED,now=now,reason="EXCHANGE_TRUTH_REQUIRED"))
        return tuple(recovered)
    def exposure_blocked(self):
        with self._connect() as db:
            marks=",".join("?" for _ in _UNRESOLVED)
            row=db.execute(f"SELECT COUNT(*) n FROM execution_lifecycle WHERE state IN ({marks})",tuple(x.value for x in _UNRESOLVED)).fetchone()
        return int(row["n"])>0
    def assert_new_exposure_allowed(self):
        if self.exposure_blocked():raise ExecutionBlocked("UNRESOLVED_EXECUTION_STATE")
