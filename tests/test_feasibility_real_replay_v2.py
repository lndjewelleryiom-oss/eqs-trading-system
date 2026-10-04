from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest

from quant_system.research.feasibility_archive_v2 import FeasibilityBar, FeasibilityFunding
from quant_system.research.feasibility_real_replay_v2 import run_fixed_campaign_replay


ROOT = Path(__file__).resolve().parents[1]
CAMPAIGN_PATH = ROOT / "research" / "preregistrations" / "v2" / "FEAS-BINANCE-BTC-MA-001.json"


def campaign() -> dict:
    return json.loads(CAMPAIGN_PATH.read_text(encoding="utf-8"))


def oscillating_validation_bars(regimes: int = 26) -> tuple[FeasibilityBar, ...]:
    start = datetime(2025, 7, 1, tzinfo=timezone.utc)
    price = Decimal("100")
    closes: list[Decimal] = []
    for regime in range(regimes):
        step = Decimal("0.35") if regime % 2 == 0 else Decimal("-0.35")
        for _ in range(20):
            price += step
            closes.append(price)
    closes.extend([price] * 80)
    bars: list[FeasibilityBar] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_ = previous if index else close
        bars.append(
            FeasibilityBar(
                open_time=start + timedelta(minutes=5 * index),
                open=open_, high=max(open_, close) + Decimal("0.2"),
                low=min(open_, close) - Decimal("0.2"), close=close,
                volume=Decimal("1000"),
            )
        )
        previous = close
    return tuple(bars)


def trending_bars(count: int = 100) -> tuple[FeasibilityBar, ...]:
    start = datetime(2025, 7, 1, tzinfo=timezone.utc)
    bars = []
    price = Decimal("100")
    for index in range(count):
        open_ = price
        price += Decimal("0.2")
        bars.append(
            FeasibilityBar(
                open_time=start + timedelta(minutes=5 * index),
                open=open_, high=price + Decimal("0.1"), low=open_ - Decimal("0.1"),
                close=price, volume=Decimal("1000"),
            )
        )
    return tuple(bars)


def test_real_replay_engine_mechanics_pass_on_fixed_validation_fixture() -> None:
    bars = oscillating_validation_bars()
    result = run_fixed_campaign_replay(bars=bars, funding=(), campaign_record=campaign())
    assert result.validation_completed_trades >= 20
    assert result.shakedown_eligible_non_qualifying is True
    assert result.blockers == ()
    assert result.final_position_quantity == 0
    assert result.pending_order_count == 0
    assert result.core_journal_reconciled is True
    assert result.funding_journal_reconciled is True
    assert result.broker_submission_enabled is False
    assert result.live_authority is False
    assert result.counts_toward_168h_100_trade_gate is False
    assert result.r13_certification_authority is False


def test_funding_settles_before_same_timestamp_next_bar_fill() -> None:
    bars = trending_bars()
    # First signal forms after bar 47 and fills at bar 48 open. Funding at that exact
    # timestamp must see the pre-fill flat position; the next funding timestamp sees long.
    funding = (
        FeasibilityFunding(bars[48].open_time, Decimal("0.001")),
        FeasibilityFunding(bars[49].open_time, Decimal("0.001")),
    )
    result = run_fixed_campaign_replay(bars=bars, funding=funding, campaign_record=campaign())
    assert len(result.funding_journal) == 1
    row = result.funding_journal[0]
    assert row.funding_time == bars[49].open_time
    assert row.position_quantity == 1
    assert row.cashflow < 0
    assert result.funding_journal_reconciled is True


def test_positive_funding_cost_lowers_adjusted_equity_when_long() -> None:
    bars = trending_bars()
    funding = tuple(
        FeasibilityFunding(bars[index].open_time, Decimal("0.001"))
        for index in range(49, 70, 4)
    )
    result = run_fixed_campaign_replay(bars=bars, funding=funding, campaign_record=campaign())
    assert result.funding_cashflow < 0
    assert result.final_adjusted_equity < result.final_core_equity


def test_oos_bar_is_hard_rejected() -> None:
    bar = FeasibilityBar(
        open_time=datetime(2026, 4, 1, tzinfo=timezone.utc),
        open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
        close=Decimal("100"), volume=Decimal("1000"),
    )
    with pytest.raises(ValueError, match="outside train/validation"):
        run_fixed_campaign_replay(bars=(bar,), funding=(), campaign_record=campaign())


def test_result_hash_record_is_deterministic() -> None:
    bars = oscillating_validation_bars()
    first = run_fixed_campaign_replay(bars=bars, funding=(), campaign_record=campaign()).to_record()
    second = run_fixed_campaign_replay(bars=bars, funding=(), campaign_record=campaign()).to_record()
    assert first == second
    assert len(first["result_sha256"]) == 64
