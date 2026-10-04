from __future__ import annotations
from datetime import datetime,timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT/'src') not in sys.path: sys.path.insert(0,str(ROOT/'src'))
from quant_system.research.feasibility_v2 import load_and_validate
from quant_system.research.feasibility_campaign2_v2 import load_campaign2
from quant_system.research.feasibility_campaign3_v2 import load_campaign3
from quant_system.research.feasibility_real_replay_v2 import load_acquired_train_validation,run_fixed_campaign_replay
from quant_system.research.feasibility_trial_ledger_v2 import FeasibilityTrialLedger
from quant_system.research.exploratory_sizing_v3 import ExploratorySizingPolicyV3

METHODOLOGY_PATH=ROOT/'research'/'preregistrations'/'v3'/'FEASIBILITY-V3-METHODOLOGY.json'
def canonical(v): return json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def main():
    acquisition=Path(r'H:\My Drive\EQS\feasibility_v2')
    manifest=acquisition/'FEAS-BINANCE-BTC-MA-001-acquisition-manifest.json'
    methodology=json.loads(METHODOLOGY_PATH.read_text(encoding='utf-8'))
    method_fp=methodology['methodology_fingerprint']
    bars_record,bars,funding=load_acquired_train_validation(project_root=ROOT,acquisition_root=acquisition)
    campaigns=[
      load_and_validate(ROOT/'research'/'preregistrations'/'v2'/'FEAS-BINANCE-BTC-MA-001.json',project_root=ROOT),
      load_campaign2(ROOT),load_campaign3(ROOT)]
    ledger=FeasibilityTrialLedger(acquisition/'control'/'feasibility_trials_v2.db')
    summaries=[]
    try:
      for record in campaigns:
        source=record['campaign']; source_fp=record['campaign_fingerprint']
        diag_id='V3-DIAG-'+source['campaign_id']
        diag_fp=sha256(canonical({'methodology_fingerprint':method_fp,'source_campaign_fingerprint':source_fp,'diagnostic_id':diag_id})).hexdigest()
        config_sha=sha256(canonical({'methodology':methodology,'source_campaign':record})).hexdigest()
        trial_id=str(uuid4())
        ledger.register_trial(trial_id=trial_id,campaign_id=diag_id,campaign_fingerprint=diag_fp,configuration_sha256=config_sha,acquisition_manifest_sha256=sha256(manifest.read_bytes()).hexdigest())
        ledger.transition(trial_id,'RUNNING',outcomes_consumed=True)
        try:
          result=run_fixed_campaign_replay(bars=bars,funding=funding,campaign_record=record,sizing_policy=ExploratorySizingPolicyV3())
          rr=result.to_record()
          envelope={
            'schema_id':'EQS-FEASIBILITY-V3-SIZING-DIAGNOSTIC-V1','trial_id':trial_id,'diagnostic_campaign_id':diag_id,
            'created_at':datetime.now(timezone.utc).isoformat().replace('+00:00','Z'),'methodology_fingerprint':method_fp,
            'source_campaign_id':source['campaign_id'],'source_campaign_fingerprint':source_fp,'diagnostic_fingerprint':diag_fp,
            'classification':'METHODOLOGY_CORRECTION_DIAGNOSTIC_ALREADY_CONSUMED_WINDOW','empirical_outcomes_consumed':True,
            'clean_validation_claim':False,'result':rr,'r13_certification_authority':False,'j25_j26_candidate_authority':False,
            'counts_toward_168h_100_trade_gate':False,'broker_submission_enabled':False,'live_authority':False}
          envelope['envelope_sha256']=sha256(canonical(envelope)).hexdigest()
          ledger.persist_result(trial_id,envelope);ledger.transition(trial_id,'FINISHED',outcomes_consumed=True)
          path=acquisition/'results'/f'{trial_id}.json'; path.write_text(json.dumps(envelope,indent=2,sort_keys=True)+'\n',encoding='utf-8')
          summaries.append({'source_campaign_id':source['campaign_id'],'trial_id':trial_id,'total_trades':result.total_completed_trades,'validation_trades':result.validation_completed_trades,'final_adjusted_equity':str(result.final_adjusted_equity),'commissions':str(result.commissions),'shakedown_eligible_non_qualifying':result.shakedown_eligible_non_qualifying,'blockers':list(result.blockers),'envelope_sha256':envelope['envelope_sha256']})
        except Exception as exc:
          ledger.transition(trial_id,'BLOCKED',error_code=type(exc).__name__,outcomes_consumed=True);raise
    finally: ledger.close()
    print(json.dumps({'status':'PASS','classification':'METHODOLOGY_DIAGNOSTIC_ONLY','clean_validation_claim':False,'summaries':summaries,'live_authority':False},sort_keys=True))
    return 0
if __name__=='__main__': raise SystemExit(main())
