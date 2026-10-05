from __future__ import annotations

from hashlib import sha256
import json

import pytest

from quant_system.research.backtest_governance_v1 import build_backtest_research_policy, build_data_eligibility_matrix
from quant_system.research.backtest_pipeline_v1 import BacktestPipelineLedger, BacktestStage, PipelineBlocked, bind_pipeline
from quant_system.research.strategy_preregistration_v1 import StrategyPreregistration


def reseal(record):
    body = dict(record)
    body.pop("record_sha256", None)
    body["record_sha256"] = sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return body


def fixtures(tmp_path, *, eligible):
    policy = build_backtest_research_policy(source_commit="abc")
    matrix = build_data_eligibility_matrix(evidence_dir=tmp_path, source_commit="abc")
    if eligible:
        rows = [dict(row) for row in matrix["rows"]]
        rows[0]["genuine_empirical_research_allowed"] = True
        rows[0]["training_allowed"] = True
        rows[0]["validation_allowed"] = True
        rows[0]["locked_oos_allowed"] = True
        rows[0]["status"] = "ELIGIBLE"
        matrix = dict(matrix)
        matrix["rows"] = rows
        matrix["eligible_genuine_asset_classes"] = ["CRYPTO_PERPETUALS"]
        matrix = reseal(matrix)
    prereg = StrategyPreregistration(
        strategy_id="CRYPTO-TREND-TEST", strategy_version="v1", family="trend",
        asset_class="CRYPTO_PERPETUALS", hypothesis="test hypothesis", economic_rationale="test rationale",
        timeframe="5m", signal_definition="frozen signal", entry_rules=("next event",), exit_rules=("exit rule",),
        risk_rules=("risk rule",), parameter_space={"lookback": [24, 48]},
        data_scope={"training": "T", "validation": "V", "oos": "O"},
        acceptance_criteria={"net_positive": True}, rejection_criteria=("net not positive",),
        robustness_tests=("2x costs",), max_family_trials=10, max_parameter_combinations=20, random_seed=1,
        research_policy_sha256=policy["record_sha256"], eligibility_matrix_sha256=matrix["record_sha256"],
    )
    return policy, matrix, prereg


def test_current_fail_closed_matrix_blocks_genuine_pipeline(tmp_path):
    policy, matrix, prereg = fixtures(tmp_path, eligible=False)
    with pytest.raises(PipelineBlocked) as exc:
        bind_pipeline(prereg, policy=policy, matrix=matrix)
    assert exc.value.code == "BLOCKED_GENUINE_R13_ARCHIVE_NOT_ADMITTED"


def test_stage_order_cannot_be_skipped(tmp_path):
    policy, matrix, prereg = fixtures(tmp_path, eligible=True)
    binding = bind_pipeline(prereg, policy=policy, matrix=matrix)
    ledger = BacktestPipelineLedger(tmp_path / "pipeline.db")
    ledger.create(binding)
    with pytest.raises(PipelineBlocked, match="STAGE_ORDER_VIOLATION"):
        ledger.record_stage(prereg.lineage_id, BacktestStage.VALIDATION, outcome="PASS", evidence_sha256="a" * 64)


def test_full_pipeline_order_is_append_only_and_hash_chained(tmp_path):
    policy, matrix, prereg = fixtures(tmp_path, eligible=True)
    binding = bind_pipeline(prereg, policy=policy, matrix=matrix)
    ledger = BacktestPipelineLedger(tmp_path / "pipeline.db")
    ledger.create(binding)
    for index, stage in enumerate(BacktestStage):
        ledger.record_stage(prereg.lineage_id, stage, outcome="PASS", evidence_sha256=f"{index:064x}")
    status = ledger.status(prereg.lineage_id)
    assert status["passed_stage_count"] == len(tuple(BacktestStage))
    assert status["next_stage"] is None
    assert ledger.verify_hash_chain()


def test_rejection_is_terminal(tmp_path):
    policy, matrix, prereg = fixtures(tmp_path, eligible=True)
    binding = bind_pipeline(prereg, policy=policy, matrix=matrix)
    ledger = BacktestPipelineLedger(tmp_path / "pipeline.db")
    ledger.create(binding)
    ledger.record_stage(prereg.lineage_id, BacktestStage.TRAINING, outcome="REJECT", evidence_sha256=None)
    assert ledger.status(prereg.lineage_id)["terminal_outcome"] == "REJECT"
    with pytest.raises(PipelineBlocked, match="PIPELINE_TERMINAL"):
        ledger.record_stage(prereg.lineage_id, BacktestStage.TRAINING, outcome="PASS", evidence_sha256="a" * 64)


def test_oos_requires_all_prior_passes(tmp_path):
    policy, matrix, prereg = fixtures(tmp_path, eligible=True)
    binding = bind_pipeline(prereg, policy=policy, matrix=matrix)
    ledger = BacktestPipelineLedger(tmp_path / "pipeline.db")
    ledger.create(binding)
    for stage in (BacktestStage.TRAINING, BacktestStage.PARAMETER_ROBUSTNESS, BacktestStage.VALIDATION):
        ledger.record_stage(prereg.lineage_id, stage, outcome="PASS", evidence_sha256="b" * 64)
    with pytest.raises(PipelineBlocked, match="STAGE_ORDER_VIOLATION"):
        ledger.record_stage(prereg.lineage_id, BacktestStage.LOCKED_OOS, outcome="PASS", evidence_sha256="c" * 64)
