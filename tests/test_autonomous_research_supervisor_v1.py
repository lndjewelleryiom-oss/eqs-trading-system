from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
import json

from quant_system.research.autonomous_supervisor_v1 import AutonomousResearchSupervisor, SupervisorExecutionResult
from quant_system.research.backtest_governance_v1 import build_backtest_research_policy, build_data_eligibility_matrix
from quant_system.research.strategy_preregistration_v1 import StrategyPreregistration


def reseal(record):
    body=dict(record); body.pop("record_sha256",None)
    body["record_sha256"]=sha256(json.dumps(body,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()
    return body


def inputs(tmp_path, *, eligible=False):
    policy=build_backtest_research_policy(source_commit="abc")
    matrix=build_data_eligibility_matrix(evidence_dir=tmp_path,source_commit="abc")
    if eligible:
        rows=[dict(r) for r in matrix["rows"]]
        rows[0]["genuine_empirical_research_allowed"]=True
        rows[0]["status"]="ELIGIBLE"
        matrix=dict(matrix); matrix["rows"]=rows; matrix["eligible_genuine_asset_classes"]=["CRYPTO_PERPETUALS"]; matrix=reseal(matrix)
    p=StrategyPreregistration(
        strategy_id="CRYPTO-TREND-001",strategy_version="v1",family="trend",asset_class="CRYPTO_PERPETUALS",
        hypothesis="trend may persist",economic_rationale="bounded test rationale",timeframe="5m",signal_definition="frozen",
        entry_rules=("next event",),exit_rules=("exit",),risk_rules=("bounded",),parameter_space={"lookback":[24]},
        data_scope={"training":"T","validation":"V","oos":"O"},acceptance_criteria={"positive":True},
        rejection_criteria=("negative",),robustness_tests=("2x costs",),max_family_trials=3,max_parameter_combinations=3,
        random_seed=1,research_policy_sha256=policy["record_sha256"],eligibility_matrix_sha256=matrix["record_sha256"])
    return matrix,p


def test_ineligible_data_is_parked_not_executed(tmp_path):
    matrix,p=inputs(tmp_path,eligible=False)
    supervisor=AutonomousResearchSupervisor(tmp_path/"s.db")
    jobs=supervisor.reconcile([p],matrix=matrix)
    assert jobs[0].state=="BLOCKED"
    assert jobs[0].blocker=="BLOCKED_GENUINE_R13_ARCHIVE_NOT_ADMITTED"
    assert supervisor.process_one(lambda job: SupervisorExecutionResult("COMPLETE","x","y")) is None
    assert supervisor.verify_hash_chain()


def test_eligible_data_queues_and_completes(tmp_path):
    matrix,p=inputs(tmp_path,eligible=True)
    supervisor=AutonomousResearchSupervisor(tmp_path/"s.db")
    jobs=supervisor.reconcile([p],matrix=matrix)
    assert jobs[0].state=="QUEUED"
    outcome=supervisor.process_one(lambda job: SupervisorExecutionResult("COMPLETE","evidence:1","PIPELINE_COMPLETE"))
    assert outcome["result"]=="COMPLETE"
    assert supervisor.get(jobs[0].job_id).state=="COMPLETE"
    assert supervisor.verify_hash_chain()


def test_blocked_job_becomes_queued_only_when_matrix_turns_eligible(tmp_path):
    matrix,p=inputs(tmp_path,eligible=False)
    supervisor=AutonomousResearchSupervisor(tmp_path/"s.db")
    job=supervisor.reconcile([p],matrix=matrix)[0]
    assert job.state=="BLOCKED"
    eligible_matrix,p2=inputs(tmp_path,eligible=True)
    # The frozen preregistration binding changes with a new matrix, so this must
    # be a new strategy version rather than mutating the old lineage.
    p2=replace(p2,strategy_version="v2")
    jobs=supervisor.reconcile([p2],matrix=eligible_matrix)
    assert any(j.lineage_id.endswith(":v2") and j.state=="QUEUED" for j in jobs)


def test_failures_retry_bounded_then_block(tmp_path):
    matrix,p=inputs(tmp_path,eligible=True)
    supervisor=AutonomousResearchSupervisor(tmp_path/"s.db",max_attempts=2)
    job=supervisor.reconcile([p],matrix=matrix)[0]
    first=supervisor.process_one(lambda j: SupervisorExecutionResult("FAILED","err:1","TRANSIENT"))
    assert first["result"]=="FAILED"
    assert supervisor.get(job.job_id).state=="QUEUED"
    second=supervisor.process_one(lambda j: SupervisorExecutionResult("FAILED","err:2","TRANSIENT"))
    assert second["result"]=="FAILED"
    assert supervisor.get(job.job_id).state=="BLOCKED"
