from pathlib import Path
import json,sqlite3,sys
from quant_system.runtime.idempotency import DurableCycleEventLedger
root=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app");out=root/"artifacts"/"commissioning"/"durable_cycle_idempotency_v1";out.mkdir(parents=True,exist_ok=True);db=out/"ledger.db";venue="BYBIT_LINEAR";ids=("e1","e2","e3");phase=sys.argv[1]
if phase=="reset":
 for x in (db,Path(str(db)+"-wal"),Path(str(db)+"-shm")):
  if x.exists():x.unlink()
 print("reset")
elif phase=="admit":
 l=DurableCycleEventLedger(db);a=l.admit(venue,ids);print(json.dumps({"fresh":len(a.fresh_event_ids),"dup":len(a.duplicate_event_ids),"duplicate_cycle":a.duplicate_cycle,"counts":l.counts(venue)}))
elif phase=="crash_pre":
 l=DurableCycleEventLedger(db);c=l._connect()
 try:
  c.execute("BEGIN IMMEDIATE");cid=l.cycle_id(venue,ids);c.execute("INSERT INTO cycles VALUES(?,?,datetime('now'),?)",(venue,cid,len(ids)));c.execute("INSERT INTO events VALUES(?,?,?,datetime('now'))",(venue,ids[0],cid));raise OSError("INJECTED_PRE_COMMIT")
 except OSError:c.rollback();print(json.dumps({"counts":l.counts(venue),"rolled_back":True}))
 finally:c.close()
elif phase=="crash_post":
 l=DurableCycleEventLedger(db);a=l.admit(venue,ids);raise SystemExit(95)
elif phase=="verify":
 l=DurableCycleEventLedger(db);a=l.admit(venue,ids);print(json.dumps({"fresh":len(a.fresh_event_ids),"dup":len(a.duplicate_event_ids),"duplicate_cycle":a.duplicate_cycle,"counts":l.counts(venue)}))
