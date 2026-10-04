from pathlib import Path
from datetime import datetime, timezone
import hashlib, json, os, time, sys
import msvcrt

ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app")
EV=ROOT/"artifacts"/"test-evidence"
sys.path.insert(0,str(ROOT/"src"))
from quant_system.operations.canonical_snapshot import load_snapshot, publish_snapshot
T=ROOT/"tracker.json"
SNAPSHOT=EV/"EQS_CANONICAL_TRACKER_SNAPSHOT_V4.json"
WRITER_LOCK=EV/".eqs_canonical_tracker_v4_writer.lck"

def load(p):
    p=Path(p)
    return json.loads(p.read_text(encoding="utf-8"))

def canonical(o):
    return json.dumps(o,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()

def atomic_json(path, value):
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

tracker=load(T)
previous_generation=0
try:
    existing_snapshot=load_snapshot(SNAPSHOT)
except FileNotFoundError:
    existing_snapshot=None
except Exception as exc:
    raise SystemExit("EXISTING_CANONICAL_SNAPSHOT_INVALID:"+type(exc).__name__)
if existing_snapshot is not None:
    previous_generation=int(existing_snapshot.get("projection_generation",0) or 0)
    if previous_generation<0:
        raise SystemExit("EXISTING_CANONICAL_SNAPSHOT_GENERATION_INVALID")
generation=previous_generation+1
tracker["canonical_projection_schema"]="EQS-CANONICAL-PROJECTION-V1"
tracker["canonical_projection_generation"]=generation
tracker["canonical_projection_owner"]="V4_BASE"
tracker["canonical_projection_updated_at"]=datetime.now(timezone.utc).isoformat().replace("+00:00","Z")
by={x.get("id"):x for x in tracker.get("components",[])}

r12_projection=EV/"EQS_R1_2_PROGRESS_PROJECTION.json"
if r12_projection.is_file():
    r12=load(r12_projection)
    r12_body=dict(r12)
    r12_seal=r12_body.pop("record_sha256",None)
    if not isinstance(r12_seal,str) or hashlib.sha256(canonical(r12_body)).hexdigest()!=r12_seal:
        raise SystemExit("R1_2_PROGRESS_PROJECTION_SEAL_INVALID")
    if r12.get("component_id")!="R1.2" or not isinstance(r12.get("component"),dict):
        raise SystemExit("R1_2_PROGRESS_PROJECTION_INVALID")
    by["R1.2"].update(r12["component"])

health=load(EV/"EQS_SUSTAINED_PAPER_HEALTH_LATEST.json")
eq2pre=load(EV/"EQS02_PRE_PAPER_STAGE_CERTIFICATION.json")
eq2auth=load(EV/"EQS02_ALPACA_AUTHENTICATED_ACCEPTANCE.json")
fxpre=load(EV/"EQS03_FX_PRE_PAPER_CERTIFICATION.json")
fxf2=load(Path(r"C:\Users\lndje\Documents\EQS\EQS-03-FX\histdata\output\202608_v1_4\AUDJPY_DUKASCOPY_GAP_BRIDGE_V1_4_SEAL.json"))
rates_src=load(Path(r"C:\Users\lndje\Documents\EQS\EQS-05\live_scan\yield_curve\TREASURY_PAR_YIELD_CURVE_2026_SEAL.json"))
rates_acc=load(EV/"EQS05_RATES_GENUINE_CURVE_ACCEPTANCE.json")
xau_pre=load(EV/"XAUUSD_SPOT_PRE_PAPER_CERTIFICATION.json")
s03ext=load(EV/"EQS_SHARED03_COMMODITY_SPOT_EXTENSION_SEAL.json")
s06=load(EV/"EQS_SHARED06_FINAL_READINESS_SEAL.json")
decision=load(EV/"EQS00_PAPER_ADMISSION_DECISION.json")

by["PP2"].update({
    "status":"IN PROGRESS",
    "evidence":[
        "sustained PAPER runtime generation="+",".join(map(str,health.get("generation_set",[]))),
        "latest sustained health status="+health.get("status","UNKNOWN"),
        "latest sustained health record="+health.get("record_sha256",""),
        "broker sends since current authority="+str(health.get("broker_sent_true_count_since_current_authorisation",0)),
        "venue submission enabled events="+str(health.get("venue_submission_enabled_true_count_since_current_authorisation",0)),
        "current PAPER admission decision="+decision.get("decision_sha256",""),
        "SHARED-06 final readiness seal="+s06.get("seal_sha256",""),
    ],
    "remaining":[
        "continue accumulating sustained PAPER operating history",
        "keep all runtime leases, data, reconciliation and safety checks green",
    ],
})

by["EQS-02-PAPER"].update({
    "status":"IN PROGRESS",
    "evidence":[
        "EQS-02 foundation/connectors/PAPER adapter ready",
        "pre-PAPER certification seal="+eq2pre["seal_sha256"],
        "authenticated Alpaca IEX production market-data acceptance PASS seal="+eq2auth["seal_sha256"],
        "production PAPER market-data authority=true",
        "authenticated universe includes SPY, AAPL and GLD",
        "Alpaca connector latest/historical normalization regression 7/7 PASS",
        "raw-first immutable capture enabled; credential material not recorded",
        "trading endpoint not used; broker submission disabled",
    ],
    "remaining":[
        "complete genuine two-observation forward Alpaca IEX acceptance after US market opens",
    ],
})

by["EQS-03-PAPER"].update({
    "status":"IN PROGRESS",
    "evidence":[
        "HistData fixed EST/no-DST source convention verified",
        "1,770 false weekend gaps reclassified",
        "1,136 bounded exact-1-second source-order corrupt intervals normalized",
        "AUDJPY missing interval repaired only with genuine Dukascopy tick coverage",
        "FX F2 PASS 15/15; remaining missing intervals=0; remaining corrupt intervals=0",
        "FX F2 v1.4 seal="+fxf2["seal_sha256"],
        "FX PAPER adapter tests 9/9 PASS",
        "FX pre-PAPER certification seal="+fxpre["seal_sha256"],
        "spot-FX PAPER simulator does not fabricate consolidated exchange volume or market impact",
    ],
    "remaining":[
        "complete genuine two-observation forward FX PAPER acceptance when Sunday FX data opens",
    ],
})

by["EQS-04-PAPER"].update({
    "status":"PAUSED",
    "evidence":[
        "CME-grade futures data commissioning deliberately deferred on cost grounds",
        "CME public website delayed quotes are not being used as certification data",
        "no paid CME DataMine subscription authorised",
    ],
    "remaining":[
        "resume only if a free/low-cost admissible licensed futures data source is identified and approved",
        "do not purchase CME DataMine without explicit approval",
    ],
})

by["EQS-05-PAPER"].update({
    "status":"IN PROGRESS",
    "evidence":[
        "EQS-05 auction foundation PASS",
        "official U.S. Treasury daily par-yield curve source PASS seal="+rates_src["seal_sha256"],
        "official curve history contains "+str(rates_src["row_count"])+" rows through "+rates_src["latest_date"],
        "Rates curve/PIT/DV01 regression 9/9 PASS",
        "genuine official-curve offline acceptance PASS seal="+rates_acc["seal_sha256"],
        "historical PIT authority not inferred; forward publication required",
        "broker submission disabled",
    ],
    "remaining":[
        "complete genuine forward Treasury curve publication acceptance on the first official curve newer than 2026-10-02",
    ],
})

by["XAU-SPOT-PAPER"].update({
    "status":"IN PROGRESS",
    "evidence":[
        "XAU/USD direct gold route implemented as COMMODITY SPOT, not FX and not CME futures",
        "genuine Dukascopy XAU/USD source acceptance PASS",
        "XAU/USD spot pre-PAPER certification PASS seal="+xau_pre["seal_sha256"],
        "SHARED-03 commodity spot extension PASS seal="+s03ext["seal_sha256"],
        "SHARED-03 extension full regression 790 passed / 1 skipped",
        "no CME dependency; no fabricated exchange volume or market impact",
        "broker submission disabled",
    ],
    "remaining":[
        "complete genuine two-observation forward XAU/USD spot PAPER acceptance when Sunday spot source opens",
    ],
})

tracker["components"]=list(by.values())
allowed=tracker.setdefault("allowed_statuses",[])
if "PAUSED" not in allowed: allowed.append("PAUSED")
snapshot={
    "schema_id":"EQS-CANONICAL-TRACKER-SNAPSHOT-V4",
    "projection_generation":generation,
    "projection_stage":"V4_BASE",
    "updated_at":tracker["canonical_projection_updated_at"],
    "tracker":tracker,
}
snapshot["record_sha256"]=hashlib.sha256(canonical(snapshot)).hexdigest()
publish_snapshot(SNAPSHOT,snapshot)
legacy_tracker_updated=False
legacy_tracker_sync_pending=True

rec={
    "schema_id":"EQS-CANONICAL-TRACKER-RECONCILIATION-V4",
    "projection_generation":generation,
    "projection_stage":"V4_BASE",
    "canonical_snapshot_sha256":snapshot["record_sha256"],
    "legacy_tracker_updated":legacy_tracker_updated,
    "legacy_tracker_sync_pending":legacy_tracker_sync_pending,
    "updated_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
    "states":{k:by[k]["status"] for k in ("PP1","PP2","EQS-02-PAPER","EQS-03-PAPER","EQS-04-PAPER","EQS-05-PAPER","XAU-SPOT-PAPER")},
    "paper_admission_decision_sha256":decision["decision_sha256"],
    "shared06_final_readiness_seal_sha256":s06["seal_sha256"],
    "shared03_commodity_spot_extension_seal_sha256":s03ext["seal_sha256"],
    "eqs02_alpaca_auth_acceptance_seal_sha256":eq2auth["seal_sha256"],
    "eqs03_pre_paper_seal_sha256":fxpre["seal_sha256"],
    "eqs05_offline_acceptance_seal_sha256":rates_acc["seal_sha256"],
    "xauusd_pre_paper_seal_sha256":xau_pre["seal_sha256"],
}
rec["record_sha256"]=hashlib.sha256(canonical(rec)).hexdigest()
atomic_json(EV/"EQS_CANONICAL_TRACKER_RECONCILIATION_V4.json",rec)
print(json.dumps({"status":"PASS","record_sha256":rec["record_sha256"],"states":rec["states"]},sort_keys=True))
