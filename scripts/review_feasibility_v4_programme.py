from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
EV=ROOT/"artifacts"/"test-evidence"
V4=ROOT/"research"/"preregistrations"/"v4"
CAMPAIGNS=("FEAS-V4-PREMIUM-MR-001","FEAS-V4-FUNDING-BASIS-002")


def canonical(value: object) -> bytes:
    return json.dumps(value,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode("utf-8")


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def valid_record(doc: dict) -> bool:
    body=dict(doc); expected=body.pop("record_sha256",None)
    return isinstance(expected,str) and sha256(canonical(body)).hexdigest()==expected


def main() -> int:
    methodology=load(V4/"METHODOLOGY-V4.json")
    maximum=int(methodology["research_rules"]["maximum_campaigns"])
    if maximum!=len(CAMPAIGNS):
        raise SystemExit("V4_CAMPAIGN_BUDGET_MISMATCH")
    trials=[]
    for campaign_id in CAMPAIGNS:
        path=EV/f"EQS_{campaign_id.replace('-', '_')}_ASSESSMENT.json"
        doc=load(path)
        if not valid_record(doc):
            raise SystemExit(f"V4_ASSESSMENT_SEAL_INVALID:{campaign_id}")
        if doc.get("campaign_id")!=campaign_id:
            raise SystemExit(f"V4_ASSESSMENT_IDENTITY_MISMATCH:{campaign_id}")
        if doc.get("status")!="BLOCKED" or doc.get("paper_entry_decision",{}).get("eligible_managed_paper_candidate") is not False:
            raise SystemExit(f"V4_CAMPAIGN_NOT_REJECTED:{campaign_id}")
        trials.append({
            "campaign_id":campaign_id,
            "trial_id":doc["trial_id"],
            "assessment_record_sha256":doc["record_sha256"],
            "training_completed_trades":doc["activity"]["training_completed_trades"],
            "validation_completed_trades":doc["activity"]["validation_completed_trades"],
            "training_net_pnl_after_costs":doc["economics"]["training_net_pnl_after_costs"],
            "validation_net_pnl_after_costs":doc["economics"]["validation_net_pnl_after_costs"],
            "whole_window_net_pnl_after_costs":doc["economics"]["whole_window_net_pnl_after_costs"],
            "commission_cost_share_of_gross_profit_fraction":doc["economics"]["commission_cost_share_of_gross_profit_fraction"],
            "maximum_drawdown_fraction":doc["economics"]["maximum_drawdown_fraction"],
            "blockers":doc["paper_entry_decision"]["blockers"],
        })
    record={
        "schema_id":"EQS-FEASIBILITY-V4-PROGRAMME-REVIEW-V1",
        "observed_at":datetime.now(timezone.utc).isoformat().replace("+00:00","Z"),
        "status":"STOPPED_NO_MANAGED_PAPER_CANDIDATE",
        "campaign_budget":maximum,
        "campaigns_consumed":len(trials),
        "exit_rule":methodology["research_rules"]["on_no_candidate"],
        "exit_rule_triggered":True,
        "conclusions":[
            "Both pre-frozen V4 campaigns failed the exploratory managed-PAPER entry screen.",
            "Both campaigns were active enough to evaluate but remained net negative after the frozen fee/slippage model.",
            "Transaction costs remained a material fraction of gross positive trade PnL, and campaign 1 also exceeded the frozen drawdown limit.",
            "No V4 parameter tuning or third V4 campaign is permitted after these outcomes.",
        ],
        "next_scope_constraints":[
            "Any successor must be a new preregistered programme/version before its outcomes are observed.",
            "Successor should materially lower decision/trade frequency rather than tune V4 thresholds.",
            "Reused train/validation data remains exploratory and cannot be relabelled a clean holdout.",
            "Locked OOS remains sealed.",
            "All prior trials remain counted.",
        ],
        "trials":trials,
        "safety":{
            "broker_submission_enabled":False,
            "live_authority":False,
            "r13_certification_authority":False,
            "j25_j26_candidate_authority":False,
            "locked_oos_opened":False,
            "counts_toward_168h_100_trade_gate":False,
        },
        "next_action":"DESIGN_NEW_LOWER_FREQUENCY_PREOUTCOME_EXPLORATORY_PROGRAMME_OR_STOP_RESEARCH_SCOPE",
    }
    record["record_sha256"]=sha256(canonical(record)).hexdigest()
    out=EV/"EQS_FEASIBILITY_V4_PROGRAMME_REVIEW.json"
    out.write_text(json.dumps(record,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"status":record["status"],"record_sha256":record["record_sha256"],"next_action":record["next_action"]},sort_keys=True))
    return 0

if __name__=="__main__":
    raise SystemExit(main())
