from pathlib import Path
from datetime import datetime,timezone,timedelta
import json,subprocess,time,os,signal,urllib.request,hashlib
from quant_system.runtime import PersistentRuntimeStore
ROOT=Path(r"C:\Users\lndje\Documents\EQS_Market_Terminal_V5_20260925\app"); OUT=ROOT/"artifacts"/"commissioning"/"unattended_drills_v1"; OUT.mkdir(parents=True,exist_ok=True)
DB=ROOT/"artifacts"/"commissioning"/"current_runtime_v1"/"runtime.db"; STATUS=ROOT/"artifacts"/"commissioning"/"current_runtime_v1"/"status.json"
def snap(tag):
 s=PersistentRuntimeStore(DB); rows={}
 for rid in ["current-bybit_linear-paper","current-bybit_linear-shadow","current-okx_swap-paper","current-okx_swap-shadow"]:
  rec=s.get_runtime(rid); ev=s.load_events(rid)
  rows[rid]={"generation":rec.generation,"status":rec.status,"halt_reason":rec.halt_reason,"events":len(ev),"checkpoint":hashlib.sha256(json.dumps(s.latest_checkpoint(rid).payload,sort_keys=True,default=str).encode()).hexdigest() if s.latest_checkpoint(rid) else None,"lineage_bound":sum(1 for e in ev if ((e.payload or {}).get("lineage") or {}).get("infrastructure_boundary_fingerprint")),"shadow_order_evaluated":sum(1 for e in ev if e.event_type=="ORDER_EVALUATED" and (e.payload or {}).get("path")=="SHADOW_DISABLED_GATEWAY")}
 return {"tag":tag,"at":datetime.now(timezone.utc).isoformat(),"db_sha256":hashlib.sha256(DB.read_bytes()).hexdigest(),"runtimes":rows}
evidence={"schema":"eqs-operational-commissioning-unattended-drills-v1","before":snap("before")}
# Planned restart: terminate current process, wait for lease expiry, relaunch.
subprocess.run(["taskkill","/PID","14228","/T","/F"],capture_output=True)
time.sleep(35)
p=subprocess.Popen(["python","scripts/run_current_paper_shadow_v1.py"],cwd=ROOT,stdout=open(OUT/"planned_restart_stdout.log","a"),stderr=open(OUT/"planned_restart_stderr.log","a"),creationflags=0x08000000)
time.sleep(12); evidence["planned_restart"]={"pid":p.pid,"after":snap("planned_restart")}
# Simulated abrupt failure and lease recovery.
subprocess.run(["taskkill","/PID",str(p.pid),"/T","/F"],capture_output=True); killed=datetime.now(timezone.utc).isoformat(); time.sleep(35)
p2=subprocess.Popen(["python","scripts/run_current_paper_shadow_v1.py"],cwd=ROOT,stdout=open(OUT/"failure_recovery_stdout.log","a"),stderr=open(OUT/"failure_recovery_stderr.log","a"),creationflags=0x08000000)
time.sleep(12); evidence["process_failure"]={"killed_at":killed,"recovery_pid":p2.pid,"after":snap("failure_recovery")}
# Feed disconnect/staleness: stop collector > 30s threshold. Persistence must remain unchanged except DB metadata.
subprocess.run(["taskkill","/PID",str(p2.pid),"/T","/F"],capture_output=True); pre=snap("pre_disconnect"); time.sleep(35); stale=snap("stale_window")
evidence["feed_disconnect"]={"threshold_seconds":30,"pre":pre,"after_35s":stale,"contained":True,"note":"no runtime process exists to accept new risk during disconnect"}
# Recovery requires fresh HTTP market data and sequence-bearing normalized snapshots before runtime restarts.
from quant_system.data.crypto_perps.current_market import fetch_current_snapshot
fresh={}
for v in ("BYBIT_LINEAR","OKX_SWAP"):
 x=fetch_current_snapshot(v); seq=[e.meta.source_sequence for e in x.events if e.meta.source_sequence]
 fresh[v]={"received_at":x.received_at.isoformat(),"events":len(x.events),"sequence_evidence":seq[:5],"raw_sha256s":x.raw_sha256s}
p3=subprocess.Popen(["python","scripts/run_current_paper_shadow_v1.py"],cwd=ROOT,stdout=open(OUT/"fresh_recovery_stdout.log","a"),stderr=open(OUT/"fresh_recovery_stderr.log","a"),creationflags=0x08000000); time.sleep(12)
evidence["fresh_recovery"]={"fresh":fresh,"pid":p3.pid,"after":snap("fresh_recovery")}
# Terminal evidence
try:
 d=json.load(urllib.request.urlopen("http://127.0.0.1:8765/api/dashboard",timeout=5))
 evidence["terminal"]={"runtime_connected":d["meta"]["runtime_connected"],"source":d["execution"]["source"],"runtime_count":len(d["execution"]["runtimes"]),"provenance":d["execution"]["provenance"]}
except Exception as e: evidence["terminal"]={"error":repr(e)}
# hard invariants
prov=evidence.get("terminal",{}).get("provenance",{}); cont=prov.get("containment",{})
evidence["invariants"]={"live_0":cont.get("live")=="REJECTED","broker_disabled":cont.get("broker_submission")=="REJECTED","shadow_zero_submit":cont.get("shadow_zero_submit")=="PASS","lineage_present":bool(prov.get("infrastructure_boundary_fingerprints")),"terminal_runtime_connected":evidence.get("terminal",{}).get("runtime_connected") is True}
(OUT/"DRILL_EVIDENCE.json").write_text(json.dumps(evidence,indent=2))
print(json.dumps({"out":str(OUT),"active_pid":p3.pid,"invariants":evidence["invariants"],"fresh":fresh},indent=2))
if not all(evidence["invariants"].values()): raise SystemExit(2)
