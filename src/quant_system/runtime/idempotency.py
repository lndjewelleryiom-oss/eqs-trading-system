from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import json, sqlite3

@dataclass(frozen=True, slots=True)
class CycleAdmission:
    cycle_id: str
    received: int
    fresh_event_ids: tuple[str,...]
    duplicate_event_ids: tuple[str,...]
    duplicate_cycle: bool

class DurableCycleEventLedger:
    """Durable admission ledger. A cycle and all of its event identities commit atomically."""
    def __init__(self,path:str|Path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True);self._init()
    def _connect(self):
        c=sqlite3.connect(self.path,timeout=10);c.row_factory=sqlite3.Row;c.execute("PRAGMA journal_mode=WAL");c.execute("PRAGMA synchronous=FULL");c.execute("PRAGMA busy_timeout=10000");return c
    def _init(self):
        with self._connect() as c:
            c.executescript("""CREATE TABLE IF NOT EXISTS cycles(venue TEXT NOT NULL,cycle_id TEXT NOT NULL,committed_at TEXT NOT NULL,event_count INTEGER NOT NULL,PRIMARY KEY(venue,cycle_id));CREATE TABLE IF NOT EXISTS events(venue TEXT NOT NULL,event_id TEXT NOT NULL,first_cycle_id TEXT NOT NULL,committed_at TEXT NOT NULL,PRIMARY KEY(venue,event_id));""")
    @staticmethod
    def cycle_id(venue,event_ids):
        return sha256((venue+"|"+"|".join(sorted(event_ids))).encode()).hexdigest()
    def admit(self,venue:str,event_ids)->CycleAdmission:
        ids=tuple(event_ids);cid=self.cycle_id(venue,ids);now=datetime.now(timezone.utc).isoformat()
        c=self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            existing=c.execute("SELECT 1 FROM cycles WHERE venue=? AND cycle_id=?",(venue,cid)).fetchone()
            seen={r["event_id"] for r in c.execute("SELECT event_id FROM events WHERE venue=? AND event_id IN (%s)"%(",".join("?"*len(ids))),(venue,*ids)).fetchall()} if ids else set()
            fresh=tuple(i for i in ids if i not in seen);dup=tuple(i for i in ids if i in seen)
            if existing is None:
                c.execute("INSERT INTO cycles VALUES(?,?,?,?)",(venue,cid,now,len(ids)))
                for i in fresh:c.execute("INSERT INTO events VALUES(?,?,?,?)",(venue,i,cid,now))
            c.commit()
            return CycleAdmission(cid,len(ids),fresh,dup,existing is not None)
        except Exception:c.rollback();raise
        finally:c.close()
    def counts(self,venue):
        with self._connect() as c:return {"cycles":c.execute("SELECT COUNT(*) n FROM cycles WHERE venue=?",(venue,)).fetchone()["n"],"events":c.execute("SELECT COUNT(*) n FROM events WHERE venue=?",(venue,)).fetchone()["n"]}
