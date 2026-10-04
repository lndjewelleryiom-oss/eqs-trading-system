from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_system.research.feasibility_archive_v2 import FeasibilityBar, FeasibilityFunding
from quant_system.research.feasibility_v4_programme import V4Bar, V4Month, programme_next_action, run_v4_campaign
from quant_system.research.feasibility_v4_strategies import BasisFeature


def month_rows(month: str, *, premium: Decimal = Decimal("0.0001")) -> V4Month:
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=timezone.utc)
    if number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, number + 1, 1, tzinfo=timezone.utc)
    count = int((end - start).total_seconds() // 300)
    rows = []
    price = Decimal("100")
    for index in range(count):
        price += Decimal("0.02") if (index // 60) % 2 == 0 else Decimal("-0.02")
        if index and index % 997 == 0:
            price += Decimal("2")
        open_ = price - Decimal("0.01")
        ts = start + timedelta(minutes=5 * index)
        trade = FeasibilityBar(ts, open_, max(open_, price) + Decimal("0.03"), min(open_, price) - Decimal("0.03"), price, Decimal("10000"))
        feature = BasisFeature(
            premium_close=premium + Decimal(index % 11) * Decimal("0.000001"),
            mark_close=price * Decimal("1.0002"),
            index_close=price,
        )
        rows.append(V4Bar(trade=trade, feature=feature))
    funding = tuple(
        FeasibilityFunding(start + timedelta(hours=8 * i), Decimal("0.0001"))
        for i in range(int((end - start).total_seconds() // (8 * 3600)))
    )
    return V4Month(month=month, bars=tuple(rows), funding=funding)


def campaign(training_start: str, training_end: str, validation_start: str, validation_end: str):
    c = {
        "campaign_id": "FEAS-V4-TEST-001",
        "version": "4.1.0",
        "classification": "EXPLORATORY_NON_EVIDENTIARY",
        "periods": {
            "training_start": training_start,
            "training_end": training_end,
            "validation_start": validation_start,
            "validation_end": validation_end,
            "locked_oos_start": "2026-04-01T00:00:00Z",
            "locked_oos_end": "2026-09-21T23:59:59Z",
            "locked_oos_access": "SEALED_NOT_PERMITTED",
        },
        "coverage": {
            "imputation_permitted": False,
            "partial_month_use_permitted": False,
            "strategy_state_reset_after_excluded_interval": True,
        },
        "execution_model": {
            "fee_bps_per_side": 5,
            "adverse_slippage_bps_per_side": 2,
            "entry_cutoff_bars_before_segment_end": 12,
        },
        "strategy": {
            "family": "PREMIUM_BASIS_MEAN_REVERSION",
            "premium_lookback_bars": 24,
            "entry_zscore": "1.5",
            "exit_zscore": "0.25",
            "max_holding_bars": 24,
        },
        "sizing": {
            "starting_equity": "10000",
            "target_position_notional_fraction": "0.10",
            "max_gross_leverage": "1.00",
            "quantity_step": "0.00001",
        },
    }
    return {"campaign": c, "campaign_fingerprint": "f" * 64}


def test_phase_segments_reset_across_excluded_month_gap_and_finish_flat():
    months = (month_rows("2023-01"), month_rows("2023-03"), month_rows("2025-07"))
    record = campaign(
        "2023-01-01T00:00:00+00:00", "2023-03-31T23:59:59+00:00",
        "2025-07-01T00:00:00+00:00", "2025-07-31T23:59:59+00:00",
    )
    result = run_v4_campaign(campaign_record=record, months=months, trial_id="v4-test-gap")
    assert result.training.final_position_quantity == 0
    assert result.validation.final_position_quantity == 0
    assert result.training.pending_order_count == 0
    assert result.validation.pending_order_count == 0
    assert not any("SEGMENT" in blocker for blocker in result.blockers)


def test_training_and_validation_are_independent_and_nonlive():
    months = (month_rows("2023-01"), month_rows("2025-07"))
    record = campaign(
        "2023-01-01T00:00:00+00:00", "2023-01-31T23:59:59+00:00",
        "2025-07-01T00:00:00+00:00", "2025-07-31T23:59:59+00:00",
    )
    result = run_v4_campaign(campaign_record=record, months=months, trial_id="v4-test-independent")
    assert result.whole_window_net_pnl_after_costs == result.training_net_pnl_after_costs + result.validation_net_pnl_after_costs
    assert result.locked_oos_opened is False
    assert result.broker_submission_enabled is False
    assert result.live_authority is False
    assert result.r13_certification_authority is False
    assert result.j25_j26_candidate_authority is False
    assert result.counts_toward_168h_100_trade_gate is False


def test_risk_normalised_sizing_remains_bounded():
    months = (month_rows("2023-01"), month_rows("2025-07"))
    record = campaign(
        "2023-01-01T00:00:00+00:00", "2023-01-31T23:59:59+00:00",
        "2025-07-01T00:00:00+00:00", "2025-07-31T23:59:59+00:00",
    )
    result = run_v4_campaign(campaign_record=record, months=months, trial_id="v4-test-risk")
    assert result.maximum_entry_notional_fraction <= Decimal("0.10")
    assert result.maximum_observed_gross_leverage <= Decimal("1.00")
    assert result.core_journal_reconciled is True
    assert result.funding_journal_reconciled is True


def test_locked_oos_and_imputation_fail_closed():
    months = (month_rows("2023-01"), month_rows("2025-07"))
    record = campaign(
        "2023-01-01T00:00:00+00:00", "2023-01-31T23:59:59+00:00",
        "2025-07-01T00:00:00+00:00", "2025-07-31T23:59:59+00:00",
    )
    record["campaign"]["periods"]["locked_oos_access"] = "OPEN"
    try:
        run_v4_campaign(campaign_record=record, months=months, trial_id="v4-test-oos")
    except PermissionError as exc:
        assert "locked OOS" in str(exc)
    else:
        raise AssertionError("locked OOS unexpectedly accepted")

    record = campaign(
        "2023-01-01T00:00:00+00:00", "2023-01-31T23:59:59+00:00",
        "2025-07-01T00:00:00+00:00", "2025-07-31T23:59:59+00:00",
    )
    record["campaign"]["coverage"]["imputation_permitted"] = True
    try:
        run_v4_campaign(campaign_record=record, months=months, trial_id="v4-test-impute")
    except PermissionError as exc:
        assert "coverage policy" in str(exc)
    else:
        raise AssertionError("imputation unexpectedly accepted")


def test_programme_exit_rule_fires_after_final_prefrozen_campaign():
    first={"campaign_number":1,"programme_exit_rule":{"maximum_campaigns":2,"on_no_candidate":"STOP_V4_AND_REVIEW_SCOPE_WITHOUT_TUNING"}}
    final={"campaign_number":2,"programme_exit_rule":{"maximum_campaigns":2,"on_no_candidate":"STOP_V4_AND_REVIEW_SCOPE_WITHOUT_TUNING"}}
    assert programme_next_action(first,eligible_managed_paper_candidate=False)=="RETAIN_REJECTED_COUNTED_TRIAL_AND_MOVE_TO_NEXT_PREFROZEN_V4_CAMPAIGN"
    assert programme_next_action(final,eligible_managed_paper_candidate=False)=="STOP_V4_AND_REVIEW_SCOPE_WITHOUT_TUNING"
    assert programme_next_action(final,eligible_managed_paper_candidate=True)=="ADMIT_TO_NON_SUBMITTING_MANAGED_PAPER_SHAKEDOWN"
