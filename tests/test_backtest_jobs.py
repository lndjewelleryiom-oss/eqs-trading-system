from pathlib import Path

import pytest

from quant_system.operations.storage_guard import GIB, StorageGuardState
from quant_system.research.backtest_jobs import BacktestJobService, validate_request
from quant_system.research.run_registry import StrategyRunRegistry


def request(key: str = "req-1") -> dict:
    return {
        "schema_version": "EQS-BACKTEST-JOB-REQUEST-V1",
        "idempotency_key": key,
        "strategy_id": "fixture.deterministic.v1",
        "strategy_version": "1",
        "engine": "EQS_NATIVE",
        "mode": "RESEARCH_REPLAY",
        "source_id": "DETERMINISTIC_FIXTURE",
        "instrument_id": "FIXTURE-USD",
        "parameters": {"entry_event_index": 1, "exit_event_index": 5, "quantity": "2"},
        "resource_budget": {"max_output_events": 5000},
    }


def normal_guard(path: str | Path) -> StorageGuardState:
    return StorageGuardState(
        path=str(path),
        free_bytes=20 * GIB,
        free_gib=20.0,
        reserve_bytes=8 * GIB,
        critical_bytes=4 * GIB,
        allow_new_research=True,
        allow_large_downloads=True,
        state="NORMAL",
        reason="test",
    )


def blocked_guard(path: str | Path) -> StorageGuardState:
    return StorageGuardState(
        path=str(path),
        free_bytes=5 * GIB,
        free_gib=5.0,
        reserve_bytes=8 * GIB,
        critical_bytes=4 * GIB,
        allow_new_research=False,
        allow_large_downloads=False,
        state="RESEARCH_BACKPRESSURE",
        reason="test blocker",
    )


def test_request_validation_rejects_credentials_and_unknown_engines() -> None:
    bad = request()
    bad["parameters"] = {"api_key": "never"}
    with pytest.raises(ValueError, match="credentials"):
        validate_request(bad)
    bad = request()
    bad["engine"] = "NAUTILUS_UNPINNED"
    with pytest.raises(ValueError, match="EQS_NATIVE"):
        validate_request(bad)


def test_service_runs_fixture_and_returns_cursor_and_results(tmp_path: Path) -> None:
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    service = BacktestJobService(tmp_path, registry=registry, storage_guard=normal_guard)
    try:
        accepted = service.submit(request())
        assert accepted["job_status"] == "QUEUED"
        status = service.wait(accepted["run_id"])
        assert status["job_status"] == "FINISHED"
        assert status["evidence_result"] == "FIXTURE_PASS"
        assert status["broker_submission_enabled"] is False
        assert status["live_authority"] is False

        first = service.events(accepted["run_id"], after=0, limit=5)
        assert first["events"]
        second = service.events(accepted["run_id"], after=first["next_cursor"])
        assert {e["event_id"] for e in first["events"]}.isdisjoint(
            {e["event_id"] for e in second["events"]}
        )

        results = service.results(accepted["run_id"])
        assert results["hash_chain_valid"] is True
        assert results["fills"]
        assert len(results["trades"]) == 1
        assert results["counts_toward_168h_100_trade_gate"] is False
    finally:
        service.close()


def test_idempotency_returns_same_run_and_rejects_changed_request(tmp_path: Path) -> None:
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    service = BacktestJobService(tmp_path, registry=registry, storage_guard=normal_guard)
    try:
        first = service.submit(request("same"))
        service.wait(first["run_id"])
        second = service.submit(request("same"))
        assert second["run_id"] == first["run_id"]
        assert second["idempotent_replay"] is True

        changed = request("same")
        changed["parameters"] = {"entry_event_index": 2}
        with pytest.raises(ValueError, match="idempotency"):
            service.submit(changed)
        assert len(registry.list_runs()) == 1
    finally:
        service.close()


def test_storage_guard_blocks_execution_but_keeps_visible_run(tmp_path: Path) -> None:
    registry = StrategyRunRegistry(tmp_path / "runs.db")
    service = BacktestJobService(tmp_path, registry=registry, storage_guard=blocked_guard)
    try:
        accepted = service.submit(request("blocked"))
        assert accepted["job_status"] == "WAITING_RESOURCE"
        assert accepted["evidence_result"] == "BLOCKED"
        run = registry.get_run(accepted["run_id"])
        assert run["status"] == "WAITING_RESOURCE"
        events = registry.events(run["run_id"])
        assert [event["event_type"] for event in events] == ["RUN_BLOCKED"]
        assert events[0]["payload"]["broker_submission_enabled"] is False
    finally:
        service.close()
