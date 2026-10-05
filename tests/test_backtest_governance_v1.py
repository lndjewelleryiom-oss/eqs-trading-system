from __future__ import annotations

import json
from pathlib import Path

from quant_system.research.backtest_governance_v1 import (
    build_backtest_research_policy,
    build_data_eligibility_matrix,
    verify_record,
)


def _write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value), encoding="utf-8")


def test_policy_is_sealed_and_fail_closed():
    policy = build_backtest_research_policy(source_commit="abc123")
    assert verify_record(policy)
    assert policy["status"] == "ACTIVE"
    assert policy["safety"]["broker_submission_enabled"] is False
    assert policy["safety"]["live_authority"] is False
    assert policy["data_rules"]["empirical_run_requires_eligible_matrix_row"] is True
    assert policy["data_rules"]["locked_oos_single_use"] is True
    assert policy["strategy_identity_rules"]["abandoned_trials_count_toward_budget"] is True
    assert policy["promotion_rules"]["positive_total_pnl_alone_is_insufficient"] is True
    assert policy["paper_starting_floor"]["elapsed_genuine_hours"] == 168
    assert policy["paper_starting_floor"]["attributable_trades"] == 100


def test_record_tampering_fails_verification():
    policy = build_backtest_research_policy(source_commit="abc123")
    policy["safety"]["live_authority"] = True
    assert not verify_record(policy)


def test_matrix_blocks_forward_only_sources_from_empirical_research(tmp_path):
    _write(tmp_path / "R1_3_ACCEPTANCE_2026-09-22.json", {"result": "PASS"})
    _write(tmp_path / "EQS_R13_OOS_ACCESS_SEAL.json", {"status": "SEALED", "outcome_viewed": False})
    matrix = build_data_eligibility_matrix(evidence_dir=tmp_path, source_commit="abc123")
    assert verify_record(matrix)
    assert matrix["eligible_genuine_asset_classes"] == []
    rows = {row["asset_class"]: row for row in matrix["rows"]}
    assert rows["CRYPTO_PERPETUALS"]["locked_oos_state"] == "SEALED"
    assert rows["EQUITIES_ETFS"]["genuine_empirical_research_allowed"] is False
    assert rows["GOLD_SPOT_XAUUSD"]["training_allowed"] is False
    assert rows["RATES_FIXED_INCOME"]["status"] == "BLOCKED_HISTORICAL_PIT_AUTHORITY_FALSE"
    assert rows["FUTURES_COMMODITIES"]["status"] == "PAUSED_COST"
    assert rows["OPTIONS"]["status"] == "FROZEN_EXCLUDED"


def test_matrix_does_not_infer_r13_oos_seal(tmp_path):
    matrix = build_data_eligibility_matrix(evidence_dir=tmp_path, source_commit="abc123")
    rows = {row["asset_class"]: row for row in matrix["rows"]}
    assert rows["CRYPTO_PERPETUALS"]["locked_oos_state"] == "UNVERIFIED"
    assert rows["CRYPTO_PERPETUALS"]["genuine_empirical_research_allowed"] is False
