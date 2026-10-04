from __future__ import annotations
import argparse, datetime, hashlib, json, subprocess, sys
from pathlib import Path
import freeze_code_tree_v1 as codefreeze

ROOT = Path(__file__).resolve().parents[1]
PRESEAL = ROOT / "artifacts/commissioning/PRESEAL_COMMISSIONING_MANIFEST_V1.json"
OUTDIR = ROOT / "artifacts/commissioning/operational_commissioning_v1_final"
FREEZE = ROOT / "artifacts/commissioning/code_tree_freeze_v1/CODE_TREE_FREEZE.json"

def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()

def run(cmd):
    p = subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True)
    return {"command": cmd, "returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--soak-dir", required=True)
    ap.add_argument("--execute-boundary-checks", action="store_true")
    args=ap.parse_args()
    soak=(ROOT/args.soak_dir).resolve()
    hb=soak/"heartbeat.json"; receipts=soak/"feed_receipts.jsonl"; complete=soak/"COMPLETE.json"
    if not all(x.is_file() for x in (PRESEAL,hb,receipts,complete)):
        raise SystemExit("BLOCKED: required preseal/soak terminal evidence missing")
    if not FREEZE.is_file():
        raise SystemExit("BLOCKED: candidate code-tree freeze missing")
    try:
        h=json.loads(hb.read_text()); c=json.loads(complete.read_text())
        rows=[json.loads(x) for x in receipts.read_text().splitlines() if x.strip()]
        frozen=json.loads(FREEZE.read_text(encoding="utf-8"))
        start=datetime.datetime.fromisoformat(h["started_at"])
        done=datetime.datetime.fromisoformat(h["completed_at"])
        scheduled=datetime.datetime.fromisoformat(h["scheduled_end_at"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"BLOCKED: malformed terminal evidence: {exc}") from exc
    duration=(done-start).total_seconds()
    actual_sha=sha(receipts)
    sources=("BYBIT_LINEAR_BTCUSDT","OKX_SWAP_BTC_USDT")
    valid_rows=all(r.get("source") in sources and r.get("status") in ("PASS","FAIL") and r.get("received_at") for r in rows)
    identities=[(r.get("source"),r.get("received_at")) for r in rows]
    no_duplicate_receipts=len(identities)==len(set(identities))
    codefreeze.ROOT=ROOT
    current_rows=codefreeze.build_rows()
    current_tree=codefreeze.tree_sha256(current_rows)
    code_tree_matches=frozen.get("tree_sha256")==current_tree and frozen.get("files")==current_rows
    counts={s:{"PASS":sum(r.get("source")==s and r.get("status")=="PASS" for r in rows),
               "FAIL":sum(r.get("source")==s and r.get("status")=="FAIL" for r in rows)} for s in sources}
    # Collector targets one cycle every 60s. Allow 90s wall-clock spacing so normal
    # request latency/jitter does not create a false failure; anything larger fails closed.
    cadence_limit_seconds=90.0
    cadence={}
    for s in sources:
        stamps=sorted(datetime.datetime.fromisoformat(r["received_at"]) for r in rows
                      if r.get("source")==s and r.get("received_at"))
        gaps=[(b-a).total_seconds() for a,b in zip(stamps,stamps[1:])]
        start_gap=(stamps[0]-start).total_seconds() if stamps else float("inf")
        end_gap=(done-stamps[-1]).total_seconds() if stamps else float("inf")
        max_internal=max(gaps,default=0.0)
        cadence[s]={"receipts":len(stamps),"start_gap_seconds":start_gap,
                    "end_gap_seconds":end_gap,"max_internal_gap_seconds":max_internal,
                    "allowed_gap_seconds":cadence_limit_seconds,
                    "pass":start_gap<=cadence_limit_seconds and end_gap<=cadence_limit_seconds
                           and max_internal<=cadence_limit_seconds}
    checks={
      "duration_ge_24h": duration >= 86400,
      "completion_at_or_after_schedule": done >= scheduled,
      "terminal_complete": h.get("live") is False and h.get("complete") is True,
      "run_id_matches": bool(h.get("run_id")) and h.get("run_id")==c.get("run_id"),
      "complete_hash_matches": c.get("receipts_sha256")==actual_sha,
      "counts_match_heartbeat": counts==h.get("counts"),
      "counts_match_complete": counts==c.get("counts"),
      "valid_receipt_rows": bool(rows) and valid_rows,
      "no_duplicate_receipts": no_duplicate_receipts,
      "both_venues_present": all(counts[s]["PASS"]+counts[s]["FAIL"]>0 for s in sources),
      "zero_failed_receipts": all(counts[s]["FAIL"] == 0 for s in sources),
      "full_duration_cadence": all(cadence[s]["pass"] for s in sources),
      "code_tree_matches_freeze": code_tree_matches,
    }
    if not all(checks.values()):
        raise SystemExit("BLOCKED: soak acceptance failed: "+json.dumps(checks,sort_keys=True))
    boundary=None; regression=None
    if args.execute_boundary_checks:
        regression=run([sys.executable,"-m","pytest","-q"])
        boundary=run([sys.executable,"-m","quant_system.ci.infrastructure_boundary_guard"])
        if regression["returncode"] or boundary["returncode"]:
            raise SystemExit("BLOCKED: final regression/boundary check failed")
    else:
        raise SystemExit("BLOCKED: final sealing requires --execute-boundary-checks")
    pre=json.loads(PRESEAL.read_text())
    pre["classification"]="EQS_OPERATIONAL_COMMISSIONING_V1"
    pre["generated_at"]=datetime.datetime.now(datetime.timezone.utc).isoformat()
    pre["seal_status"]="PASS"
    pre["soak"]={"status":"PASS","path":args.soak_dir.replace("\\","/"),
      "heartbeat_sha256":sha(hb),"receipts_sha256":actual_sha,"complete_sha256":sha(complete),
      "started_at":h["started_at"],"completed_at":h["completed_at"],"duration_seconds":duration,
      "final_counts":counts,"cadence":cadence,"checks":checks}
    pre["code_tree_freeze"]={"status":"PASS","schema":frozen.get("schema"),
      "method":frozen.get("method"),"tree_sha256":current_tree,"file_count":len(current_rows),
      "manifest_sha256":sha(FREEZE)}
    pre["unresolved_fields"]=[]
    pre["final_regression"]={"status":"PASS","stdout":regression["stdout"].strip()}
    pre["final_boundary"]={"status":"PASS","stdout":boundary["stdout"].strip()}
    pre.pop("preseal_manifest_content_sha256",None)
    canonical=json.dumps(pre,sort_keys=True,separators=(",",":")).encode()
    pre["canonical_content_sha256"]=hashlib.sha256(canonical).hexdigest()
    OUTDIR.mkdir(parents=True,exist_ok=True)
    manifest=OUTDIR/"COMMISSIONING_MANIFEST.json"
    manifest.write_text(json.dumps(pre,indent=2),encoding="utf-8")
    seal={"classification":"EQS_OPERATIONAL_COMMISSIONING_V1_SEAL",
      "generated_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),
      "manifest_sha256":sha(manifest),"canonical_content_sha256":pre["canonical_content_sha256"],
      "hard_boundaries":pre["hard_boundaries"]}
    (OUTDIR/"SEAL.json").write_text(json.dumps(seal,indent=2),encoding="utf-8")
    print(json.dumps({"status":"PASS","manifest":str(manifest),"manifest_sha256":sha(manifest),
      "seal":str(OUTDIR/"SEAL.json"),"seal_sha256":sha(OUTDIR/"SEAL.json"),
      "counts":counts,"duration_seconds":duration},indent=2))

if __name__=="__main__":
    main()
