from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from quant_system.research.feasibility_archive_v2 import FeasibilityBar, FeasibilityFunding
from quant_system.research.feasibility_v3_programme import run_v3_campaign


def bars(start: datetime, count: int) -> tuple[FeasibilityBar, ...]:
    values = []
    price = Decimal("100")
    for index in range(count):
        # Deterministic alternating regimes with occasional large deviations.
        block = (index // 40) % 2
        price += Decimal("0.20") if block == 0 else Decimal("-0.20")
        if index % 73 == 0 and index > 0:
            price += Decimal("4") if block == 0 else Decimal("-4")
        open_ = price - Decimal("0.05")
        values.append(
            FeasibilityBar(
                open_time=start + timedelta(minutes=5 * index),
                open=open_,
                high=max(open_, price) + Decimal("0.10"),
                low=min(open_, price) - Decimal("0.10"),
                close=price,
                volume=Decimal("10000"),
            )
        )
    return tuple(values)


def campaign(training_start: datetime, training_end: datetime, validation_start: datetime, validation_end: datetime):
    c = {
        "campaign_id": "FEAS-V3-TEST-001",
        "version": "3.1.0",
        "classification": "EXPLORATORY_NON_EVIDENTIARY",
        "periods": {
            "training_start": training_start.isoformat(),
            "training_end": training_end.isoformat(),
            "validation_start": validation_start.isoformat(),
            "validation_end": validation_end.isoformat(),
            "locked_oos_start": "2026-04-01T00:00:00+00:00",
            "locked_oos_end": "2026-09-21T23:59:59+00:00",
            "locked_oos_access": "SEALED_NOT_PERMITTED",
        },
        "execution_model": {
            "fee_bps_per_side": 5,
            "adverse_slippage_bps_per_side": 2,
            "entry_cutoff_bars_before_window_end": 10,
        },
        "strategy": {
            "family": "VOLATILITY_NORMALIZED_MEAN_REVERSION",
            "lookback_bars": 20,
            "entry_zscore": "2.0",
            "exit_zscore": "0.5",
            "max_holding_bars": 12,
        },
        "sizing": {
            "starting_equity": "10000",
            "target_position_notional_fraction": "0.10",
            "max_gross_leverage": "1.00",
            "quantity_step": "0.00001",
        },
    }
    return {"campaign": c, "campaign_fingerprint": "f" * 64}


def test_two_phase_replay_resets_state_and_stays_nonlive():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    training = bars(start, 600)
    validation_start = training[-1].open_time + timedelta(minutes=5)
    validation = bars(validation_start, 400)
    all_bars = training + validation
    record = campaign(start, training[-1].open_time, validation_start, validation[-1].open_time)
    result = run_v3_campaign(campaign_record=record, bars=all_bars, funding=(), trial_id="trial-test")

    assert result.training.bar_count == 600
    assert result.validation.bar_count == 400
    assert result.training.final_position_quantity == 0
    assert result.validation.final_position_quantity == 0
    assert result.training.pending_order_count == 0
    assert result.validation.pending_order_count == 0
    assert result.core_journal_reconciled is True
    assert result.funding_journal_reconciled is True
    assert result.locked_oos_opened is False
    assert result.broker_submission_enabled is False
    assert result.live_authority is False
    assert result.r13_certification_authority is False
    assert result.j25_j26_candidate_authority is False
    assert result.counts_toward_168h_100_trade_gate is False
    assert result.maximum_entry_notional_fraction <= Decimal("0.10")
    assert result.maximum_observed_gross_leverage <= Decimal("1.00")


def test_phase_metrics_are_independent_not_cumulative():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    training = bars(start, 400)
    validation_start = training[-1].open_time + timedelta(minutes=5)
    validation = bars(validation_start, 400)
    record = campaign(start, training[-1].open_time, validation_start, validation[-1].open_time)
    result = run_v3_campaign(campaign_record=record, bars=training + validation, funding=(), trial_id="trial-test-2")
    assert result.whole_window_net_pnl_after_costs == result.training_net_pnl_after_costs + result.validation_net_pnl_after_costs
    assert result.training.final_core_equity != Decimal("0")
    assert result.validation.final_core_equity != Decimal("0")


def test_locked_oos_permission_fails_closed():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    training = bars(start, 100)
    validation_start = training[-1].open_time + timedelta(minutes=5)
    validation = bars(validation_start, 100)
    record = campaign(start, training[-1].open_time, validation_start, validation[-1].open_time)
    record["campaign"]["periods"]["locked_oos_access"] = "OPEN"
    try:
        run_v3_campaign(campaign_record=record, bars=training + validation, funding=(), trial_id="trial-test-3")
    except PermissionError as exc:
        assert "locked OOS" in str(exc)
    else:
        raise AssertionError("locked OOS boundary unexpectedly accepted")


def test_funding_events_can_be_processed_without_broker_or_live_authority():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    training = bars(start, 400)
    validation_start = training[-1].open_time + timedelta(minutes=5)
    validation = bars(validation_start, 400)
    funding = tuple(
        FeasibilityFunding(funding_time=start + timedelta(hours=8 * index), funding_rate=Decimal("0.0001"))
        for index in range(4)
    )
    record = campaign(start, training[-1].open_time, validation_start, validation[-1].open_time)
    result = run_v3_campaign(campaign_record=record, bars=training + validation, funding=funding, trial_id="trial-test-4")
    assert result.broker_submission_enabled is False
    assert result.live_authority is False
