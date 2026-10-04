from __future__ import annotations
import copy, datetime as dt, hashlib, json, subprocess, sys, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FINALIZER=ROOT/"scripts/finalize_operational_commissioning_v1.py"
FREEZE=ROOT/"artifacts/commissioning/code_tree_freeze_v1/CODE_TREE_FREEZE.json"
SOURCES=("BYBIT_LINEAR_BTCUSDT","OKX_SWAP_BTC_USDT")

def iso(t): return t.isoformat()
def write_fixture(path, mutate=None):
    start=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc); done=start+dt.timedelta(hours=24)
    rows=[{"source":s,"status":"PASS","received_at":iso(start+dt.timedelta(seconds=i))}
          for i in range(0,86401,60) for s in SOURCES]
    h={"run_id":"synthetic","started_at":iso(start),"scheduled_end_at":iso(done),"completed_at":iso(done),
       "live":False,"complete":True}
    state={"rows":rows,"h":h,"c":{"run_id":"synthetic"}}
    if mutate: mutate(state)
    rows=state["rows"]; h=state["h"]; c=state["c"]
    counts={s:{"PASS":sum(r.get("source")==s and r.get("status")=="PASS" for r in rows),
               "FAIL":sum(r.get("source")==s and r.get("status")=="FAIL" for r in rows)} for s in SOURCES}
    h.setdefault("counts",counts); c.setdefault("counts",counts)
    path.mkdir(parents=True,exist_ok=True)
    receipts=path/"feed_receipts.jsonl"
    receipts.write_text("".join(json.dumps(r,separators=(",",":"))+"\n" for r in rows))
    c.setdefault("receipts_sha256",hashlib.sha256(receipts.read_bytes()).hexdigest())
    (path/"heartbeat.json").write_text(json.dumps(h))
    (path/"COMPLETE.json").write_text(json.dumps(c))
    return path

def invoke(path):
    p=subprocess.run([sys.executable,str(FINALIZER),"--soak-dir",str(path)],cwd=ROOT,text=True,capture_output=True)
    return p.returncode,(p.stdout+p.stderr).strip()

def main():
    cases=[]
    with tempfile.TemporaryDirectory(prefix="eqs01_seal_") as td:
        td=Path(td)
        def case(name, mut, expected):
            p=write_fixture(td/name,mut); rc,out=invoke(p)
            ok=rc!=0 and expected in out
            cases.append({"case":name,"pass":ok,"returncode":rc,"expected":expected,"output":out[-1000:]})
        case("duration_23h59m",lambda x:x["h"].update(completed_at=iso(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)+dt.timedelta(hours=23,minutes=59))),"duration_ge_24h")
        case("live_true",lambda x:x["h"].update(live=True),"terminal_complete")
        case("complete_false",lambda x:x["h"].update(complete=False),"terminal_complete")
        case("bybit_fail",lambda x:x["rows"][0].update(status="FAIL"),"zero_failed_receipts")
        case("okx_fail",lambda x:x["rows"][1].update(status="FAIL"),"zero_failed_receipts")
        case("missing_venue",lambda x:x.update(rows=[r for r in x["rows"] if r["source"]!=SOURCES[1]]),"both_venues_present")
        case("wrong_hash",lambda x:x["c"].update(receipts_sha256="0"*64),"complete_hash_matches")
        case("heartbeat_count_mismatch",lambda x:x["h"].update(counts={}),"counts_match_heartbeat")
        case("complete_count_mismatch",lambda x:x["c"].update(counts={}),"counts_match_complete")
        case("cadence_120s",lambda x:x.update(rows=[r for r in x["rows"] if not (r["source"]==SOURCES[0] and r["received_at"].endswith("00:01:00+00:00"))]),"full_duration_cadence")
        case("unexpected_source",lambda x:x["rows"][0].update(source="UNKNOWN"),"valid_receipt_rows")
        case("duplicate_receipt",lambda x:x["rows"].append(copy.deepcopy(x["rows"][0])),"no_duplicate_receipts")
        case("empty_receipts",lambda x:x.update(rows=[]),"valid_receipt_rows")
        case("missing_completed_at",lambda x:x["h"].pop("completed_at"),"malformed terminal evidence")
        p=write_fixture(td/"missing_complete"); (p/"COMPLETE.json").unlink(); rc,out=invoke(p); cases.append({"case":"missing_complete","pass":rc!=0 and "terminal evidence missing" in out,"returncode":rc,"output":out[-1000:]})
        p=write_fixture(td/"malformed"); (p/"heartbeat.json").write_text("{"); rc,out=invoke(p); cases.append({"case":"malformed","pass":rc!=0 and "malformed terminal evidence" in out,"returncode":rc,"output":out[-1000:]})
        case("valid_reaches_boundary_gate",None,"final sealing requires --execute-boundary-checks")
        original=FREEZE.read_bytes(); frozen=json.loads(original); frozen["tree_sha256"]="0"*64; FREEZE.write_text(json.dumps(frozen))
        try:
            p=write_fixture(td/"tree_mismatch"); rc,out=invoke(p)
            cases.append({"case":"tree_mismatch","pass":rc!=0 and "code_tree_matches_freeze" in out,"returncode":rc,"output":out[-1000:]})
        finally: FREEZE.write_bytes(original)
    outdir=ROOT/"artifacts/commissioning/overnight_eqs01"; outdir.mkdir(parents=True,exist_ok=True)
    evidence={"classification":"EQS01_FINAL_SEAL_ADVERSARIAL_V1","cases":cases,"pass":all(x["pass"] for x in cases)}
    ep=outdir/"FINAL_SEAL_ADVERSARIAL_V1.json"; ep.write_text(json.dumps(evidence,indent=2))
    print(json.dumps({"pass":evidence["pass"],"cases":len(cases),"evidence":str(ep),"sha256":hashlib.sha256(ep.read_bytes()).hexdigest()},indent=2))
    if not evidence["pass"]: raise SystemExit(1)
if __name__=="__main__": main()
