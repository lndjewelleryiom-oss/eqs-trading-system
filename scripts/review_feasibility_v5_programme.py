from __future__ import annotations
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; EV=ROOT/"artifacts"/"test-evidence"; V5=ROOT/"research"/"preregistrations"/"v5"
CAMPAIGNS=("FEAS-V5-SLOW-TREND-001","FEAS-V5-SLOW-BREAKOUT-002")
def canonical(v): return json.dumps(v,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
def load(p): return json.loads(Path(p).read_text(encoding="utf-8"))
def valid(doc):
    body=dict(doc); expected=body.pop("record_sha256",None)
    return isinstance(expected,str) and sha256(canonical(body)).hexdigest()==expected
def main():
    methodology=load(V5/"METHODOLOGY-V5.json"); maximum=int(methodology["research_rules"]["maximum_campaigns"])
    if maximum!=len(CAMPAIGNS): raise SystemExit("V5_CAMPAIGN_BUDGET_MISMATCH")
    trials=[]
    for cid in CAMPAIGNS:
        doc=load(EV/f"EQS_{cid.replace('-', '_')}_ASSESSMENT.json")
        if not valid(doc): raise SystemExit(f"V5_ASSESSMENT_SEAL_INVALID:{cid}")
        if doc.get("campaign_id")!=cid: raise SystemExit(f"V5_ASSESSMENT_IDENTITY_MISMATCH:{cid}")
        if doc.get("paper_entry_decision",{}).get("eligible_managed_paper_candidate") is not False: raise SystemExit(f"V5_CAMPAIGN_NOT_REJECTED:{cid}")
        trials.append({"campaign_id":cid,"trial_id":doc["trial_id"],"assessment_record_sha256":doc["record_sha256"],"training_completed_trades":doc["activity"]["training_completed_trades"],"validation_completed_trades":doc["activity"]["validation_completed_trades"],**doc["economics"],"blockers":doc["paper_entry_decision"]["blockers"]})
    record={"schema_id":"EQS-FEASIBILITY-V5-PROGRAMME-REVIEW-V1","observed_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"status":"STOPPED_NO_MANAGED_PAPER_CANDIDATE","campaign_budget":maximum,"campaigns_consumed":len(trials),"exit_rule":methodology["research_rules"]["on_no_candidate"],"exit_rule_triggered":True,"conclusions":["V5 materially reduced trade frequency and transaction-cost burden compared with V3/V4.","Slow trend remained negative in both training and validation.","Slow breakout was profitable in training and whole-window aggregate but validation remained negative, so the frozen paper-entry gate correctly rejected it.","No V5 threshold change, validation relabelling, or third V5 campaign is permitted after these outcomes."],"next_scope_constraints":["Do not tune V5 slow-breakout thresholds to rescue the near miss.","Do not reuse the consumed validation window as a clean holdout.","Any new strategy programme must be a materially distinct pre-registered hypothesis or use genuinely new future data.","Locked OOS remains sealed until the proper evidentiary gate is reached."],"trials":trials,"safety":{"broker_submission_enabled":False,"live_authority":False,"r13_certification_authority":False,"j25_j26_candidate_authority":False,"locked_oos_opened":False,"counts_toward_168h_100_trade_gate":False},"next_action":"PARK_SAME_WINDOW_STRATEGY_MINING_AND_PROGRESS_INDEPENDENT_SYSTEM_BLOCKERS_OR_GENUINELY_NEW_DATA_SCOPE"}
    record["record_sha256"]=sha256(canonical(record)).hexdigest(); out=EV/"EQS_FEASIBILITY_V5_PROGRAMME_REVIEW.json"; out.write_text(json.dumps(record,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"status":record["status"],"record_sha256":record["record_sha256"],"next_action":record["next_action"]},sort_keys=True)); return 0
if __name__=="__main__": raise SystemExit(main())
