from __future__ import annotations

import json
from pathlib import Path

from quant_system.research.paper_entry_gate_v1 import assess_paper_entry, load_policy

ROOT = Path(__file__).resolve().parents[1]


def methodology():
    return json.loads((ROOT / "research" / "preregistrations" / "v3" / "FEASIBILITY-V3-METHODOLOGY.json").read_text())["methodology"]


def policy():
    return load_policy(ROOT / "research" / "preregistrations" / "v3" / "PAPER-ENTRY-V1.json")


def passing_result():
    return {
        "campaign_id": "FEAS-V3-INDEPENDENT-001",
        "trial_id": "trial-001",
        "classification": "EXPLORATORY_NON_EVIDENTIARY",
        "training_completed_trades": 80,
        "validation_completed_trades": 30,
        "training_net_pnl_after_costs": "350",
        "validation_net_pnl_after_costs": "120",
        "whole_window_net_pnl_after_costs": "470",
        "maximum_drawdown_fraction": "0.08",
        "cost_share_of_gross_profit_fraction": "0.31",
        "maximum_observed_gross_leverage": "0.10",
        "maximum_position_notional_fraction": "0.10",
        "core_journal_reconciled": True,
        "funding_journal_reconciled": True,
        "final_position_quantity": "0",
        "pending_order_count": 0,
        "shakedown_eligible_non_qualifying": True,
        "locked_oos_opened": False,
        "broker_submission_enabled": False,
        "live_authority": False,
        "r13_certification_authority": False,
        "j25_j26_candidate_authority": False,
        "counts_toward_168h_100_trade_gate": False,
    }


def passing_trial():
    return {
        "trial_id": "trial-001",
        "campaign_id": "FEAS-V3-INDEPENDENT-001",
        "source_class": "PUBLIC_UNADMITTED_EXPLORATORY",
        "state": "FINISHED",
        "empirical_outcomes_consumed": 1,
    }


def test_positive_counted_exploratory_candidate_can_enter_managed_paper_only():
    decision = assess_paper_entry(result=passing_result(), trial=passing_trial(), methodology=methodology(), policy=policy())
    assert decision.status == "MANAGED_PAPER_CANDIDATE"
    assert decision.eligible_managed_paper_candidate is True
    assert decision.blockers == ()
    assert decision.broker_submission_enabled is False
    assert decision.live_authority is False
    assert decision.r13_certification_authority is False
    assert decision.j25_j26_candidate_authority is False
    assert decision.counts_historical_trades_toward_168h_100_trade_gate is False


def test_negative_validation_economics_blocks_paper_entry():
    result = passing_result()
    result["validation_net_pnl_after_costs"] = "-1"
    decision = assess_paper_entry(result=result, trial=passing_trial(), methodology=methodology(), policy=policy())
    assert decision.status == "BLOCKED"
    assert "VALIDATION_NET_PNL_NOT_POSITIVE" in decision.blockers


def test_excess_leverage_or_drawdown_blocks_paper_entry():
    result = passing_result()
    result["maximum_observed_gross_leverage"] = "1.01"
    result["maximum_drawdown_fraction"] = "0.21"
    decision = assess_paper_entry(result=result, trial=passing_trial(), methodology=methodology(), policy=policy())
    assert "MAXIMUM_GROSS_LEVERAGE_EXCEEDED" in decision.blockers
    assert "MAXIMUM_DRAWDOWN_EXCEEDED" in decision.blockers


def test_unfinished_or_uncounted_trial_blocks_paper_entry():
    trial = passing_trial()
    trial["state"] = "RUNNING"
    trial["empirical_outcomes_consumed"] = 0
    decision = assess_paper_entry(result=passing_result(), trial=trial, methodology=methodology(), policy=policy())
    assert "TRIAL_NOT_FINISHED" in decision.blockers
    assert "TRIAL_OUTCOME_CONSUMPTION_NOT_RECORDED" in decision.blockers


def test_locked_oos_or_live_authority_is_rejected():
    result = passing_result()
    result["locked_oos_opened"] = True
    result["live_authority"] = True
    result["r13_certification_authority"] = True
    decision = assess_paper_entry(result=result, trial=passing_trial(), methodology=methodology(), policy=policy())
    assert "LOCKED_OOS_MUST_REMAIN_CLOSED" in decision.blockers
    assert "LIVE_AUTHORITY_NOT_FALSE" in decision.blockers
    assert "R13_AUTHORITY_NOT_FALSE" in decision.blockers


def test_insufficient_activity_or_accounting_failure_blocks():
    result = passing_result()
    result["training_completed_trades"] = 49
    result["validation_completed_trades"] = 19
    result["core_journal_reconciled"] = False
    result["pending_order_count"] = 1
    decision = assess_paper_entry(result=result, trial=passing_trial(), methodology=methodology(), policy=policy())
    assert "MINIMUM_TRAINING_TRADES_NOT_MET" in decision.blockers
    assert "MINIMUM_VALIDATION_TRADES_NOT_MET" in decision.blockers
    assert "JOURNAL_RECONCILIATION_NOT_PASS" in decision.blockers
    assert "PENDING_ORDERS_NOT_ZERO" in decision.blockers
