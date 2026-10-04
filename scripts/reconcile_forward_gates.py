from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import hashlib
import json
import os
import time
import msvcrt
import sys

ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
EV=ROOT/"artifacts"/"test-evidence"
sys.path.insert(0,str(ROOT/"src"))
from quant_system.operations.canonical_snapshot import load_snapshot, publish_snapshot
TRACKER=ROOT/"tracker.json"
SNAPSHOT=EV/"EQS_CANONICAL_TRACKER_SNAPSHOT_V4.json"
OUT=EV/"EQS_FORWARD_GATE_RECONCILIATION.json"
WRITER_LOCK=EV/".eqs_canonical_tracker_v4_writer.lck"

def canonical(o):
    return json.dumps(o,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()

def atomic_json(path,value):
    path=Path(path)
    tmp=path.with_suffix(path.suffix+".tmp")
    with tmp.open("w",encoding="utf-8",newline="") as handle:
        json.dump(value,handle,indent=2,sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    last=None
    for attempt in range(30):
        try:
            os.replace(tmp,path)
            return
        except PermissionError as exc:
            last=exc
            time.sleep(0.1*(attempt+1))
    try:
        tmp.unlink(missing_ok=True)
    finally:
        raise last

def load(path):
    p=Path(path)
    if not p.is_file(): return None
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return None

def valid_seal(doc):
    if not isinstance(doc,dict) or not isinstance(doc.get("seal_sha256"),str): return False
    core=dict(doc); expected=core.pop("seal_sha256")
    return hashlib.sha256(canonical(core)).hexdigest()==expected

_writer_lock_handle=WRITER_LOCK.open("a+b")
_writer_lock_handle.seek(0,os.SEEK_END)
if _writer_lock_handle.tell()==0:
    _writer_lock_handle.write(b"0")
    _writer_lock_handle.flush()
_writer_lock_handle.seek(0)
_writer_lock_acquired=False
for _attempt in range(100):
    try:
        msvcrt.locking(_writer_lock_handle.fileno(),msvcrt.LK_NBLCK,1)
        _writer_lock_acquired=True
        break
    except OSError:
        time.sleep(0.05)
if not _writer_lock_acquired:
    raise SystemExit("CANONICAL_TRACKER_WRITER_BUSY")

try:
    snapshot=load_snapshot(SNAPSHOT)
except FileNotFoundError:
    raise SystemExit("CANONICAL_SNAPSHOT_REQUIRED")
except Exception as exc:
    raise SystemExit("CANONICAL_SNAPSHOT_INVALID:"+type(exc).__name__)
if snapshot.get("projection_stage")!="V4_BASE":
    raise SystemExit("FORWARD_PROJECTION_REQUIRES_FRESH_V4_BASE")
tracker=snapshot.get("tracker")
if not isinstance(tracker,dict):
    raise SystemExit("TRACKER_INVALID")
if tracker.get("canonical_projection_schema")!="EQS-CANONICAL-PROJECTION-V1":
    raise SystemExit("CANONICAL_PROJECTION_SCHEMA_REQUIRED")
if tracker.get("canonical_projection_owner")!="V4_BASE":
    raise SystemExit("FORWARD_PROJECTION_REQUIRES_FRESH_V4_BASE")
generation=int(snapshot.get("projection_generation",0) or 0)
if generation<=0:
    raise SystemExit("CANONICAL_PROJECTION_GENERATION_INVALID")
tracker["canonical_projection_owner"]="V4_FORWARD"
tracker["canonical_projection_updated_at"]=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
allowed=tracker.setdefault("allowed_statuses",[])
if "PAUSED" not in allowed:
    allowed.append("PAUSED")
components=tracker.setdefault("components",[])
by={x.get("id"):x for x in components if isinstance(x,dict)}

gates={
    "EQS-02-PAPER":{
        "acceptance":EV/"EQS02_ALPACA_FORWARD_PAPER_ACCEPTANCE.json",
        "status":EV/"EQS02_ALPACA_FORWARD_PAPER_STATUS.json",
        "pass_evidence":"Genuine two-observation Alpaca IEX forward PAPER acceptance PASS",
    },
    "EQS-03-PAPER":{
        "acceptance":EV/"EQS03_FX_FORWARD_PAPER_ACCEPTANCE.json",
        "status":EV/"EQS03_FX_FORWARD_PAPER_STATUS.json",
        "pass_evidence":"Genuine two-observation FX forward PAPER acceptance PASS",
    },
    "EQS-05-PAPER":{
        "acceptance":EV/"EQS05_RATES_FORWARD_CURVE_ACCEPTANCE.json",
        "status":EV/"EQS05_RATES_FORWARD_CURVE_STATUS.json",
        "pass_evidence":"Genuine forward U.S. Treasury curve PAPER valuation acceptance PASS",
    },
}
for cid,spec in gates.items():
    row=by.get(cid)
    if row is None: continue
    acc=load(spec["acceptance"])
    stat=load(spec["status"])
    if valid_seal(acc) and acc.get("result")=="PASS":
        row["status"]="PASSED"
        ev=list(row.get("evidence",[]))
        line=spec["pass_evidence"]+" seal="+acc["seal_sha256"]
        if line not in ev: ev.append(line)
        row["evidence"]=ev
        row["remaining"]=[]
    else:
        if row.get("status") != "PAUSED":
            row["status"]="IN PROGRESS"
        state=stat.get("state") if isinstance(stat,dict) else "NOT_RUN"
        row["remaining"]=["forward acceptance pending; current state="+str(state)]

xau=load(EV/"XAUUSD_FORWARD_SPOT_PAPER_ACCEPTANCE.json")
xau_status=load(EV/"XAUUSD_FORWARD_SPOT_PAPER_STATUS.json")
xau_pre=load(EV/"XAUUSD_SPOT_PRE_PAPER_CERTIFICATION.json")
xau_row=by.get("XAU-SPOT-PAPER")
if xau_row is None:
    xau_row={
        "id":"XAU-SPOT-PAPER","name":"Gold XAU/USD spot PAPER route",
        "status":"IN PROGRESS","depends_on":["PP1"],"evidence":[],"remaining":[]}
    components.append(xau_row); by[xau_row["id"]]=xau_row
if valid_seal(xau_pre) and xau_pre.get("result")=="PASS":
    ev=list(xau_row.get("evidence",[]))
    line="XAU/USD spot pre-PAPER certification PASS seal="+xau_pre["seal_sha256"]
    if line not in ev: ev.append(line)
    xau_row["evidence"]=ev
if valid_seal(xau) and xau.get("result")=="PASS":
    xau_row["status"]="PASSED"
    ev=list(xau_row.get("evidence",[]))
    line="Genuine two-observation XAU/USD spot PAPER acceptance PASS seal="+xau["seal_sha256"]
    if line not in ev: ev.append(line)
    xau_row["evidence"]=ev
    xau_row["remaining"]=[]
else:
    xau_row["status"]="IN PROGRESS"
    state=xau_status.get("state") if isinstance(xau_status,dict) else "NOT_RUN"
    xau_row["remaining"]=["forward acceptance pending; current state="+str(state)]

if "EQS-04-PAPER" in by:
    by["EQS-04-PAPER"]["status"]="PAUSED"
    by["EQS-04-PAPER"]["remaining"]=[
        "resume only if a free/low-cost admissible licensed futures data source is identified and approved",
        "do not purchase CME DataMine without explicit approval",
    ]

snapshot={
    "schema_id":"EQS-CANONICAL-TRACKER-SNAPSHOT-V4",
    "projection_generation":generation,
    "projection_stage":"V4_FORWARD",
    "updated_at":tracker["canonical_projection_updated_at"],
    "tracker":tracker,
}
snapshot["record_sha256"]=hashlib.sha256(canonical(snapshot)).hexdigest()
publish_snapshot(SNAPSHOT,snapshot)
legacy_tracker_updated=False
legacy_tracker_sync_pending=True

record={
    "schema_id":"EQS-FORWARD-GATE-RECONCILIATION-V2",
    "projection_generation":generation,
    "projection_stage":"V4_FORWARD",
    "canonical_snapshot_sha256":snapshot["record_sha256"],
    "legacy_tracker_updated":legacy_tracker_updated,
    "legacy_tracker_sync_pending":legacy_tracker_sync_pending,
    "updated_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
    "states":{cid:by[cid]["status"] for cid in ("EQS-02-PAPER","EQS-03-PAPER","EQS-04-PAPER","EQS-05-PAPER","XAU-SPOT-PAPER") if cid in by},
}
record["record_sha256"]=hashlib.sha256(canonical(record)).hexdigest()
atomic_json(OUT,record)

v4_path=EV/"EQS_CANONICAL_TRACKER_RECONCILIATION_V4.json"
v4=load(v4_path)
if not isinstance(v4,dict) or int(v4.get("projection_generation",0) or 0)!=generation:
    raise SystemExit("V4_RECONCILIATION_GENERATION_MISMATCH")
v4["projection_stage"]="V4_FORWARD"
v4["canonical_snapshot_sha256"]=snapshot["record_sha256"]
v4["legacy_tracker_updated"]=legacy_tracker_updated
v4["updated_at"]=record["updated_at"]
v4["states"].update(record["states"])
v4.pop("record_sha256",None)
v4["record_sha256"]=hashlib.sha256(canonical(v4)).hexdigest()
atomic_json(v4_path,v4)
print(json.dumps({"status":"PASS","states":record["states"],"record_sha256":record["record_sha256"]},sort_keys=True))
