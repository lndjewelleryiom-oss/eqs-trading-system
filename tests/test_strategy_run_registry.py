from pathlib import Path

import pytest

from quant_system.research.replay_fixture import run_fixture_replay
from quant_system.research.run_registry import StrategyRunRegistry


def test_registry_rejects_broker_connected_modes(tmp_path: Path) -> None:
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    with pytest.raises(ValueError, match="broker-connected"):
        registry.create_run(
            run_id="live-run",
            strategy_id="x",
            strategy_version="1",
            mode="LIVE",
            source_id="fixture",
            instrument_id="X",
            parameters={},
        )


def test_fixture_replay_is_inspectable_hash_chained_and_nonbroker(tmp_path: Path) -> None:
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    run = run_fixture_replay(registry, "fixture-run-001")

    assert run["status"] == "FINISHED"
    assert run["result"] == "FIXTURE_PASS"
    assert run["mode"] == "RESEARCH_REPLAY"
    assert run["fill_provenance"] == "SIMULATED"
    assert run["broker_submission_enabled"] is False
    assert run["live_authority"] is False
    assert registry.verify_chain(run["run_id"])

    events = registry.events(run["run_id"])
    types = [event["event_type"] for event in events]
    assert types[0] == "RUN_STARTED"
    assert types[-1] == "RUN_FINISHED"
    for required in (
        "MARKET_BAR", "SIGNAL", "RISK_DECISION", "ORDER_SUBMITTED",
        "FILL", "POSITION_CHANGE", "TRADE_CLOSED", "EQUITY_UPDATE",
        "RUN_PROGRESS",
    ):
        assert required in types

    fills = [event for event in events if event["event_type"] == "FILL"]
    assert fills
    assert all(event["payload"]["fill_provenance"] == "SIMULATED" for event in fills)
    submitted = [event for event in events if event["event_type"] == "ORDER_SUBMITTED"]
    assert all(event["payload"]["broker_submitted"] is False for event in submitted)

    finish = events[-1]["payload"]
    assert finish["journal_reconciled"] is True
    assert finish["counts_toward_168h_100_trade_gate"] is False
    assert finish["completed_economic_trades"] == 1


def test_event_cursor_is_resumable_without_duplicates(tmp_path: Path) -> None:
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    run_fixture_replay(registry, "fixture-run-002")
    all_events = registry.events("fixture-run-002")
    split = all_events[len(all_events) // 2]["sequence"]

    first = registry.events("fixture-run-002", after=0, limit=split)
    second = registry.events("fixture-run-002", after=split)
    assert first[-1]["sequence"] == split
    assert second[0]["sequence"] == split + 1
    assert [x["event_id"] for x in first + second] == [x["event_id"] for x in all_events]


def test_completed_run_cannot_be_relabelled(tmp_path: Path) -> None:
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    run_fixture_replay(registry, "fixture-run-003")
    with pytest.raises(ValueError, match="terminal"):
        registry.set_status("fixture-run-003", "RUNNING")
