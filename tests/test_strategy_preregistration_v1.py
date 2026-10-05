from __future__ import annotations

from dataclasses import replace

import pytest

from quant_system.research.strategy_preregistration_v1 import (
    StrategyPreregistration,
    StrategyPreregistrationLedger,
)


H = "a" * 64


def prereg(**overrides):
    value = StrategyPreregistration(
        strategy_id="XAU-TREND-001",
        strategy_version="v1",
        family="trend",
        asset_class="GOLD_SPOT_XAUUSD",
        hypothesis="Volatility-adjusted breakouts can produce persistent forward returns after costs.",
        economic_rationale="Gold can exhibit persistence after information and volatility shocks.",
        timeframe="5m",
        signal_definition="Close exceeds a preregistered volatility-adjusted breakout threshold.",
        entry_rules=("enter next eligible event after signal",),
        exit_rules=("time or reversal exit",),
        risk_rules=("bounded notional", "precommitted stop"),
        parameter_space={"lookback": [24, 48, 72], "threshold": [1.0, 1.5, 2.0]},
        data_scope={"training": "sealed-later", "validation": "sealed-later", "oos": "sealed-later"},
        acceptance_criteria={"validation_net_positive": True, "oos_net_positive": True},
        rejection_criteria=("validation net <= 0", "isolated optimum"),
        robustness_tests=("parameter neighbours", "2x costs"),
        max_family_trials=2,
        max_parameter_combinations=5,
        random_seed=7,
        research_policy_sha256=H,
        eligibility_matrix_sha256=H,
    )
    return replace(value, **overrides) if overrides else value


def test_preregistration_fingerprint_is_deterministic():
    assert prereg().fingerprint == prereg().fingerprint


def test_rejects_authority_escalation():
    with pytest.raises(ValueError, match="broker or LIVE"):
        prereg(live_authority=True)


def test_ledger_is_idempotent_for_exact_frozen_content(tmp_path):
    ledger = StrategyPreregistrationLedger(tmp_path / "registry.db")
    first = ledger.register(prereg())
    second = ledger.register(prereg())
    assert first["fingerprint"] == second["fingerprint"]


def test_same_lineage_cannot_be_mutated_after_registration(tmp_path):
    ledger = StrategyPreregistrationLedger(tmp_path / "registry.db")
    ledger.register(prereg())
    changed = prereg(hypothesis="Changed after seeing results")
    with pytest.raises(ValueError, match="different frozen content"):
        ledger.register(changed)


def test_trial_and_parameter_budgets_count_all_reserved_attempts(tmp_path):
    ledger = StrategyPreregistrationLedger(tmp_path / "registry.db")
    ledger.register(prereg())
    lineage = prereg().lineage_id
    ledger.reserve_trial(lineage, "t1", parameter_combinations=2)
    ledger.close_trial(lineage, "t1", outcome="REJECTED")
    ledger.reserve_trial(lineage, "t2", parameter_combinations=3)
    ledger.close_trial(lineage, "t2", outcome="ABANDONED")
    assert ledger.trial_counts(lineage) == {"trials": 2, "parameter_combinations": 5}
    with pytest.raises(RuntimeError, match="family trial budget"):
        ledger.reserve_trial(lineage, "t3", parameter_combinations=1)


def test_closure_requires_reserved_trial(tmp_path):
    ledger = StrategyPreregistrationLedger(tmp_path / "registry.db")
    ledger.register(prereg())
    with pytest.raises(ValueError, match="reserved"):
        ledger.close_trial(prereg().lineage_id, "not-reserved", outcome="FAILED")
