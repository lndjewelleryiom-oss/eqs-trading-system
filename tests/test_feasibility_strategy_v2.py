from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from quant_system.research.feasibility_strategy_v2 import (
    FixedSmaCrossover,
    run_synthetic_shakedown,
    synthetic_shakedown_bars,
)
from quant_system.research.run_registry import StrategyRunRegistry


ROOT = Path(__file__).resolve().parents[1]


def projection(registry: StrategyRunRegistry, run_id: str) -> list[tuple[str, str | None, dict]]:
    return [
        (event["event_type"], event["market_time_utc"], event["payload"])
        for event in registry.events(run_id, limit=5000)
    ]


def test_fixed_sma_has_no_signal_before_slow_window() -> None:
    strategy = FixedSmaCrossover(fast_bars=12, slow_bars=48, max_holding_bars=12)
    for value in range(47):
        assert strategy.observe(Decimal(100 + value), Decimal("0")) is None
    signal = strategy.observe(Decimal("200"), Decimal("0"))
    assert signal is not None
    assert signal[0] == 1


def test_synthetic_shakedown_runs_end_to_end_without_broker(tmp_path: Path) -> None:
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    run = run_synthetic_shakedown(project_root=ROOT, registry=registry, run_id="feas-v2-synthetic-001")
    assert run["status"] == "FINISHED"
    assert run["result"] == "SHAKEDOWN_MECHANICS_PASS"
    assert run["broker_submission_enabled"] is False
    assert run["live_authority"] is False
    assert registry.verify_chain(run["run_id"])

    events = registry.events(run["run_id"], limit=5000)
    finish = events[-1]
    assert finish["event_type"] == "RUN_FINISHED"
    assert finish["payload"]["completed_economic_trades"] >= 20
    assert finish["payload"]["journal_reconciled"] is True
    assert finish["payload"]["counts_toward_168h_100_trade_gate"] is False
    assert finish["payload"]["eligible_for_r13_certification"] is False
    assert finish["payload"]["eligible_for_j25_j26"] is False

    orders = [event for event in events if event["event_type"] == "ORDER_SUBMITTED"]
    fills = [event for event in events if event["event_type"] == "FILL"]
    assert orders and fills
    assert all(event["payload"]["broker_submitted"] is False for event in orders)
    order_time = {event["payload"]["order_id"]: event["market_time_utc"] for event in orders}
    assert all(event["market_time_utc"] > order_time[event["payload"]["order_id"]] for event in fills)


def test_shakedown_replay_is_deterministic_at_strategy_projection(tmp_path: Path) -> None:
    first = StrategyRunRegistry(tmp_path / "a.db")
    second = StrategyRunRegistry(tmp_path / "b.db")
    run_synthetic_shakedown(project_root=ROOT, registry=first, run_id="same-run")
    run_synthetic_shakedown(project_root=ROOT, registry=second, run_id="same-run")
    assert projection(first, "same-run") == projection(second, "same-run")


def test_fixture_never_uses_locked_oos_market_dates() -> None:
    bars = synthetic_shakedown_bars()
    assert bars
    assert max(bar.timestamp for bar in bars).year == 2026
    assert max(bar.timestamp for bar in bars).month == 1
