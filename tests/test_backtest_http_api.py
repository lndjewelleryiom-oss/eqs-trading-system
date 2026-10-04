from __future__ import annotations

import json
from pathlib import Path
import runpy
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quant_system.operations.storage_guard import GIB, StorageGuardState
from quant_system.research.backtest_jobs import BacktestJobService
from quant_system.research.run_registry import StrategyRunRegistry


ROOT = Path(__file__).resolve().parents[1]


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


def body(key: str = "http-1") -> dict:
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


def _json(url: str) -> dict:
    with urlopen(url, timeout=2) as response:
        return json.loads(response.read())


def test_http_backtest_api_is_local_bounded_and_resumable(tmp_path: Path) -> None:
    namespace = runpy.run_path(
        str(ROOT / "scripts" / "serve_research_backtests.py"),
        run_name="eqs_j51_http_test",
    )
    handler = namespace["ResearchBacktestHandler"]
    server_cls = namespace["ThreadingHTTPServer"]
    service = BacktestJobService(
        tmp_path,
        registry=StrategyRunRegistry(tmp_path / "runs.db"),
        storage_guard=normal_guard,
    )
    handler.service = service
    server = server_cls(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        health = _json(base + "/healthz")
        assert health["broker_submission_enabled"] is False
        assert health["live_authority"] is False
        assert health["storage_state"] == "NORMAL"

        request = Request(
            base + "/backtests",
            data=json.dumps(body()).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "http-1"},
            method="POST",
        )
        with urlopen(request, timeout=2) as response:
            accepted = json.loads(response.read())
            assert response.status == 202
        run_id = accepted["run_id"]
        service.wait(run_id)

        status = _json(base + f"/strategy-runs/{run_id}")
        assert status["job_status"] == "FINISHED"
        assert status["broker_submission_enabled"] is False

        first = _json(base + f"/strategy-runs/{run_id}/events?after=0&limit=5")
        second = _json(base + f"/strategy-runs/{run_id}/events?after={first['next_cursor']}")
        assert first["events"]
        assert first["next_cursor"] < second["next_cursor"]

        results = _json(base + f"/strategy-runs/{run_id}/results")
        assert results["hash_chain_valid"] is True
        assert results["trades"]
        assert results["counts_toward_168h_100_trade_gate"] is False

        listing = _json(base + "/strategy-runs")
        assert listing["runs"][0]["run_id"] == run_id
        assert listing["broker_submission_enabled"] is False

        changed = body()
        changed["parameters"] = {"entry_event_index": 2}
        request = Request(
            base + "/backtests",
            data=json.dumps(changed).encode(),
            headers={"Content-Type": "application/json", "Idempotency-Key": "http-1"},
            method="POST",
        )
        try:
            urlopen(request, timeout=2)
        except HTTPError as exc:
            assert exc.code == 400
            error = json.loads(exc.read())
            assert "idempotency" in error["detail"]
        else:
            raise AssertionError("changed idempotent request should fail")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        service.close()
