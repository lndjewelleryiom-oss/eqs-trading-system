from __future__ import annotations

from pathlib import Path
import runpy
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from quant_system.interface import MockReadOnlyEqsAdapter

ROOT = Path(__file__).resolve().parents[1]
MASTER_SHA256 = "beb0f4eb7ec21f7a637a4d37e3a76709b53e0ee23412b7539704ed89f049ed07"


def test_read_model_is_explicitly_non_mutating_and_bound_to_master() -> None:
    snapshot = MockReadOnlyEqsAdapter(ROOT).snapshot().to_payload()
    assert snapshot["meta"]["access_mode"] == "READ_ONLY"
    assert snapshot["meta"]["mutations_enabled"] is False
    assert snapshot["command"]["master_sha256"] == MASTER_SHA256
    assert snapshot["command"]["execution_authority"] == "NON_LIVE_ONLY"
    assert snapshot["alpha_research"]["winning_strategy_selected"] is False
    assert snapshot["alpha_research"]["profitability_claimed"] is False


def test_alpha_gate_matrix_is_complete_without_fabricating_binding_readiness() -> None:
    snapshot = MockReadOnlyEqsAdapter(ROOT).snapshot().to_payload()
    gates = {gate["id"]: gate for gate in snapshot["alpha_research"]["gates"]}
    assert tuple(gates) == tuple(f"A{i:02d}" for i in range(1, 29))
    assert gates["A18"]["state"] == "BLOCKED_BY_HISTORY"
    assert gates["A21"]["state"] == "BLOCKED_BY_HISTORY"
    assert gates["A22"]["state"] == "BLOCKED_BY_HISTORY"
    assert gates["A28"]["state"] == "BOUNDARY_VERIFIED"
    assert snapshot["alpha_research"]["binding_ready"] is False
    assert len(snapshot["alpha_research"]["campaigns"]) == 6


def test_registry_stays_empty_until_empirical_research_exists() -> None:
    snapshot = MockReadOnlyEqsAdapter(ROOT).snapshot().to_payload()
    assert snapshot["strategies"]["candidates"] == []
    assert snapshot["strategies"]["survivors"] == []
    assert snapshot["strategies"]["registry_state"] == "EMPTY_BY_DESIGN"


def test_frontend_exposes_all_required_read_only_areas() -> None:
    html = (ROOT / "interface/index.html").read_text(encoding="utf-8")
    app = (ROOT / "interface/app.js").read_text(encoding="utf-8")
    for page in ("overview", "data", "research", "strategies", "risk", "runtime", "evidence", "jobs"):
        assert page in app
    assert 'id="workspace"' in html
    assert "fetch('/api/dashboard'" in app
    assert 'method:"POST"' not in app
    assert 'method:"PUT"' not in app
    assert 'method:"PATCH"' not in app
    assert 'method:"DELETE"' not in app


def test_http_server_rejects_mutation_methods() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts/serve_readonly_interface.py"), run_name="eqs_interface_test_server")
    handler = namespace["ReadOnlyInterfaceHandler"]
    server_cls = namespace["ThreadingHTTPServer"]
    server = server_cls(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        with urlopen(f"http://{host}:{port}/api/status", timeout=2) as response:
            assert response.status == 200
            assert b'"mutations_enabled": false' in response.read()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            request = Request(f"http://{host}:{port}/api/status", method=method, data=b"{}")
            try:
                urlopen(request, timeout=2)
            except HTTPError as exc:
                assert exc.code == 405
                assert exc.headers["Allow"] == "GET"
            else:
                raise AssertionError(f"{method} unexpectedly succeeded")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
