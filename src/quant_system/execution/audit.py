from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import json,sqlite3
from pathlib import Path
from uuid import UUID

@dataclass(frozen=True,slots=True)
class ExecutionAuditRecord:
    sequence:int;intent_id:UUID;event_type:str;occurred_at:datetime;evidence:dict;sha256:str

class ExecutionAuditTrail:
    """Credential-free append-only execution evidence journal."""
    def __init__(self,path:str|Path):
        self.path=Path(path);self._init()
    def _connect(self):
        c=sqlite3.connect(self.path);c.row_factory=sqlite3.Row;c.execute("PRAGMA journal_mode=WAL");c.execute("PRAGMA synchronous=FULL");return c
    def _init(self):
        with self._connect() as c:c.execute("""CREATE TABLE IF NOT EXISTS execution_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,intent_id TEXT NOT NULL,event_type TEXT NOT NULL,occurred_at TEXT NOT NULL,evidence_json TEXT NOT NULL,evidence_sha256 TEXT NOT NULL)""")
    def append(self,intent_id,event_type,occurred_at,evidence):
        forbidden={"api_key","secret","passphrase","authorization"}
        if forbidden & {str(k).lower() for k in evidence}:raise ValueError("CREDENTIAL_FIELD_FORBIDDEN")
        raw=json.dumps(evidence,sort_keys=True,separators=(",",":"));digest=sha256(raw.encode()).hexdigest()
        with self._connect() as c:c.execute("INSERT INTO execution_audit(intent_id,event_type,occurred_at,evidence_json,evidence_sha256) VALUES(?,?,?,?,?)",(str(intent_id),event_type,occurred_at.isoformat(),raw,digest))
    def records(self,intent_id):
        with self._connect() as c:rows=c.execute("SELECT * FROM execution_audit WHERE intent_id=? ORDER BY id",(str(intent_id),)).fetchall()
        return tuple(ExecutionAuditRecord(int(r["id"]),UUID(r["intent_id"]),r["event_type"],datetime.fromisoformat(r["occurred_at"]),json.loads(r["evidence_json"]),r["evidence_sha256"]) for r in rows)
