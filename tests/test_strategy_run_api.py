from __future__ import annotations

import json
from pathlib import Path
import runpy
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quant_system.research.replay_fixture import run_fixture_replay
from quant_system.research.run_reader import StrategyRunReadOnlyReader
from quant_system.research.run_registry import StrategyRunRegistry

ROOT = Path(__file__).resolve().parents[1]


def _server(tmp_path: Path):
    db = tmp_path / "runs.db"
    registry = StrategyRunRegistry(db)
    run_fixture_replay(registry, "api-fixture-001")
    namespace = runpy.run_path(
        str(ROOT / "scripts" / "serve_readonly_interface.py"),
        run_name="eqs_strategy_run_api_test",
    )
    handler = namespace["ReadOnlyInterfaceHandler"]
    server_cls = namespace["ThreadingHTTPServer"]
    handler.strategy_runs = StrategyRunReadOnlyReader(db)
    server = server_cls(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _get(base: str, path: str):
    with urlopen(base + path, timeout=2) as response:
        return response.status, json.loads(response.read())


def test_strategy_run_list_detail_events_results_and_projections(tmp_path: Path) -> None:
    server, thread = _server(tmp_path)
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        status, listing = _get(base, "/api/strategy-runs")
        assert status == 200
        assert listing["read_only"] is True
        assert listing["mutations_enabled"] is False
        assert listing["runs"][0]["run_id"] == "api-fixture-001"

        _, detail = _get(base, "/api/strategy-runs/api-fixture-001")
        assert detail["run"]["mode"] == "RESEARCH_REPLAY"
        assert detail["run"]["broker_submission_enabled"] is False

        _, events = _get(base, "/api/strategy-runs/api-fixture-001/events?after=2")
        assert events["after"] == 2
        assert events["events"][0]["sequence"] == 3

        _, results = _get(base, "/api/strategy-runs/api-fixture-001/results")
        assert results["run"]["result"] == "FIXTURE_PASS"
        assert results["trades"]
        assert results["equity"]
        assert results["market"]
        assert results["signals"]
        assert results["orders"]
        assert "events" not in results

        _, trades = _get(base, "/api/strategy-runs/api-fixture-001/trades")
        assert trades["trades"][0]["counts_toward_forward_gate"] is False
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_strategy_run_api_rejects_mutation(tmp_path: Path) -> None:
    server, thread = _server(tmp_path)
    host, port = server.server_address
    base = f"http://{host}:{port}"
    try:
        request = Request(base + "/api/strategy-runs", data=b"{}", method="POST")
        try:
            urlopen(request, timeout=2)
        except HTTPError as exc:
            assert exc.code == 405
            payload = json.loads(exc.read())
            assert payload["error"] == "READ_ONLY_INTERFACE"
            assert payload["mutations_enabled"] is False
        else:
            raise AssertionError("mutation endpoint unexpectedly accepted POST")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
