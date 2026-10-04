from __future__ import annotations

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from quant_system.research.feasibility_trial_ledger_v2 import FeasibilityTrialLedger
from quant_system.research.feasibility_v5_programme import load_campaign_v5, load_v5_months, programme_next_action, run_v5_campaign
from quant_system.research.paper_entry_gate_v1 import assess_paper_entry, load_policy

ROOT=Path(__file__).resolve().parents[1]
EV=ROOT/"artifacts"/"test-evidence"
V5=ROOT/"research"/"preregistrations"/"v5"
DEFAULT_V2_ROOT=Path(r"H:\My Drive\EQS\feasibility_v2")
DEFAULT_V4_ROOT=Path(r"H:\My Drive\EQS\feasibility_v4_basis")
DEFAULT_LEDGER=DEFAULT_V2_ROOT/"control"/"feasibility_trials_v2.db"


def canonical(value:object)->bytes:
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")


def file_sha(path:Path)->str:
    return sha256(path.read_bytes()).hexdigest()


def valid_record(doc:dict)->bool:
    body=dict(doc); expected=body.pop("record_sha256",None)
    return isinstance(expected,str) and sha256(canonical(body)).hexdigest()==expected


def write_evidence(path:Path,record:dict[str,object])->str:
    body=dict(record); body.pop("record_sha256",None); digest=sha256(canonical(body)).hexdigest(); body["record_sha256"]=digest
    path.write_text(json.dumps(body,indent=2,sort_keys=True)+"\n",encoding="utf-8"); return digest


def main()->int:
    parser=argparse.ArgumentParser(); parser.add_argument("campaign",choices=["FEAS-V5-SLOW-TREND-001","FEAS-V5-SLOW-BREAKOUT-002"])
    parser.add_argument("--v2-root",default=str(DEFAULT_V2_ROOT)); parser.add_argument("--v4-root",default=str(DEFAULT_V4_ROOT)); parser.add_argument("--ledger",default=str(DEFAULT_LEDGER)); args=parser.parse_args()
    campaign_path=V5/f"{args.campaign}.json"; campaign_record=load_campaign_v5(campaign_path); campaign=dict(campaign_record["campaign"])
    methodology_path=V5/"METHODOLOGY-V5.json"; methodology=json.loads(methodology_path.read_text(encoding="utf-8"))
    policy_path=V5/"PAPER-ENTRY-V5.json"; policy=load_policy(policy_path)
    v2_manifest=Path(args.v2_root)/"FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json"
    v4_manifest=Path(args.v4_root)/"FEAS-BINANCE-BTC-BASIS-V4-acquisition-manifest.json"
    v4_scope=ROOT/"research"/"preregistrations"/"v4"/"DATA-SCOPE-V4.json"
    v4_review_path=EV/"EQS_FEASIBILITY_V4_PROGRAMME_REVIEW.json"; v4_review=json.loads(v4_review_path.read_text(encoding="utf-8"))
    if not valid_record(v4_review) or v4_review.get("status")!="STOPPED_NO_MANAGED_PAPER_CANDIDATE": raise SystemExit("V5_REQUIRES_VALID_V4_STOP_REVIEW")
    source=dict(campaign["source_bindings"])
    observed={"v2_price_funding_manifest_file_sha256":file_sha(v2_manifest),"v4_basis_manifest_file_sha256":file_sha(v4_manifest),"v4_data_scope_sha256":file_sha(v4_scope),"v4_programme_review_record_sha256":v4_review["record_sha256"]}
    for key,value in observed.items():
        if str(source.get(key))!=str(value): raise SystemExit(f"V5_SOURCE_BINDING_MISMATCH:{key}")
    if file_sha(policy_path)!=str(campaign["paper_entry_policy_sha256"]): raise SystemExit("V5_PAPER_ENTRY_POLICY_BINDING_MISMATCH")
    if methodology.get("methodology_fingerprint")!=campaign.get("methodology_fingerprint"): raise SystemExit("V5_METHODOLOGY_BINDING_MISMATCH")
    body=dict(methodology); expected=body.pop("methodology_fingerprint",None)
    if sha256(canonical(body)).hexdigest()!=expected: raise SystemExit("V5_METHODOLOGY_FINGERPRINT_INVALID")
    configuration={"campaign_fingerprint":campaign_record["campaign_fingerprint"],"methodology_fingerprint":methodology["methodology_fingerprint"],"paper_entry_policy_sha256":file_sha(policy_path),"v2_manifest_sha256":observed["v2_price_funding_manifest_file_sha256"],"v4_manifest_sha256":observed["v4_basis_manifest_file_sha256"],"v4_review_sha256":v4_review["record_sha256"],"runner_sha256":file_sha(Path(__file__)),"programme_sha256":file_sha(ROOT/"src"/"quant_system"/"research"/"feasibility_v5_programme.py"),"strategy_sha256":file_sha(ROOT/"src"/"quant_system"/"research"/"feasibility_v5_strategies.py")}
    configuration_sha=sha256(canonical(configuration)).hexdigest(); combined_source_sha=sha256(canonical(observed)).hexdigest()
    trial_id=str(uuid5(NAMESPACE_URL,f"EQS-V5:{campaign_record['campaign_fingerprint']}:{combined_source_sha}:{configuration_sha}"))
    ledger=FeasibilityTrialLedger(args.ledger)
    try:
        trial=ledger.register_trial(trial_id=trial_id,campaign_id=str(campaign["campaign_id"]),campaign_fingerprint=str(campaign_record["campaign_fingerprint"]),configuration_sha256=configuration_sha,acquisition_manifest_sha256=combined_source_sha)
        existing=EV/f"EQS_{args.campaign.replace('-','_')}_ASSESSMENT.json"
        if trial["state"]=="FINISHED":
            if not existing.is_file(): raise SystemExit("V5_FINISHED_TRIAL_EVIDENCE_MISSING")
            print(existing.read_text(encoding="utf-8")); return 0
        if trial["state"]=="BLOCKED": raise SystemExit("V5_TRIAL_ALREADY_BLOCKED")
        if trial["state"]=="CREATED": ledger.transition(trial_id,"RUNNING",outcomes_consumed=True)
        elif trial["state"]!="RUNNING": raise SystemExit("V5_TRIAL_STATE_INVALID")
        try:
            v2_sha,v4_sha,months=load_v5_months(Path(args.v2_root),Path(args.v4_root),str(campaign["campaign_id"]))
            if v2_sha!=observed["v2_price_funding_manifest_file_sha256"] or v4_sha!=observed["v4_basis_manifest_file_sha256"]: raise RuntimeError("V5_SOURCE_CHANGED_DURING_LOAD")
            result=run_v5_campaign(campaign_record=campaign_record,months=months,trial_id=trial_id); result_record=result.to_record(); result_sha=ledger.persist_result(trial_id,result_record); ledger.transition(trial_id,"FINISHED",outcomes_consumed=True)
        except Exception as exc:
            current=ledger.trial(trial_id)
            if current["state"]=="RUNNING": ledger.transition(trial_id,"BLOCKED",error_code=type(exc).__name__,outcomes_consumed=True)
            raise
        final_trial=ledger.trial(trial_id); decision=assess_paper_entry(result=result_record,trial=final_trial,methodology=methodology,policy=policy); decision_record=decision.to_record()
        assessment={"schema_id":"EQS-FEASIBILITY-V5-CAMPAIGN-ASSESSMENT-V1","observed_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),"campaign_id":campaign["campaign_id"],"campaign_version":campaign["version"],"campaign_fingerprint":campaign_record["campaign_fingerprint"],"trial_id":trial_id,"trial_result_sha256":result_sha,"trial_ledger_hash_chain_valid":ledger.verify_hash_chain(),"trial_count_after_run":ledger.trial_count(),"classification":"EXPLORATORY_NON_EVIDENTIARY","paper_entry_decision":decision_record,"source_bindings":observed,"coverage":campaign["coverage"],"economics":{"training_net_pnl_after_costs":result_record["training_net_pnl_after_costs"],"validation_net_pnl_after_costs":result_record["validation_net_pnl_after_costs"],"whole_window_net_pnl_after_costs":result_record["whole_window_net_pnl_after_costs"],"maximum_drawdown_fraction":result_record["maximum_drawdown_fraction"],"commission_cost_share_of_gross_profit_fraction":result_record["commission_cost_share_of_gross_profit_fraction"]},"activity":{"training_completed_trades":result_record["training_completed_trades"],"validation_completed_trades":result_record["validation_completed_trades"]},"safety":{"locked_oos_opened":False,"broker_submission_enabled":False,"live_authority":False,"r13_certification_authority":False,"j25_j26_candidate_authority":False,"counts_toward_168h_100_trade_gate":False},"status":decision.status,"next_action":programme_next_action(campaign,eligible_managed_paper_candidate=decision.eligible_managed_paper_candidate)}
        result_path=EV/f"EQS_{args.campaign.replace('-','_')}_RESULT.json"; assessment_path=EV/f"EQS_{args.campaign.replace('-','_')}_ASSESSMENT.json"; write_evidence(result_path,result_record); assessment_sha=write_evidence(assessment_path,assessment)
        print(json.dumps({"status":assessment["status"],"campaign_id":campaign["campaign_id"],"trial_id":trial_id,"assessment_record_sha256":assessment_sha,"next_action":assessment["next_action"],"blockers":decision_record["blockers"],"training_net":result_record["training_net_pnl_after_costs"],"validation_net":result_record["validation_net_pnl_after_costs"],"training_trades":result_record["training_completed_trades"],"validation_trades":result_record["validation_completed_trades"]},sort_keys=True)); return 0
    finally: ledger.close()

if __name__=="__main__": raise SystemExit(main())
