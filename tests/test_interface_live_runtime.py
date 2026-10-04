from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sqlite3
import threading
from urllib.request import urlopen
from uuid import UUID

from quant_system.interface import LiveReadOnlyEqsAdapter, RuntimeStoreReadOnlyReader
from quant_system.runtime.store import PersistentRuntimeStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 23, 0, 30, tzinfo=timezone.utc)
ORDER_ID = UUID("11111111-1111-1111-1111-111111111111")
STRATEGY_ID = UUID("22222222-2222-2222-2222-222222222222")


def _order(quantity: str = "0.02") -> dict[str, object]:
    return {
        "strategy_id": str(STRATEGY_ID),
        "symbol": "BTC-USDT-PERP",
        "side": "BUY",
        "quantity": quantity,
        "order_type": "MARKET",
        "decision_time": NOW.isoformat(),
        "reference_price": "112000",
        "limit_price": None,
        "reduce_only": False,
        "order_id": str(ORDER_ID),
    }


def _paper_checkpoint() -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_id": "paper-main",
        "mode": "PAPER",
        "status": "RUNNING",
        "halt_reason": None,
        "metrics": {
            "orders_received": 2,
            "paper_fills": 1,
            "shadow_decisions": 0,
            "reconciliations": 1,
            "reconciliation_failures": 0,
            "degradation_checks": 1,
            "degradation_pauses": 0,
            "errors": 0,
            "restarts": 1,
            "recovery_attempts": 1,
            "recovery_successes": 1,
            "heartbeats": 4,
        },
        "last_market_event_at": NOW.isoformat(),
        "last_reconcile_at": NOW.isoformat(),
        "last_degradation_at": NOW.isoformat(),
        "consecutive_failures": 0,
        "paper": {
            "ledger": {
                "initial_cash": "100000",
                "cash": "99800.25",
                "positions": {
                    "BTC-USDT-PERP": {
                        "quantity": "0.01",
                        "average_cost": "112000",
                        "realized_pnl": "12.50",
                    }
                },
                "commissions": "0.75",
                "financing_costs": "0",
                "borrow_costs": "0",
                "dividend_cashflow": "0",
                "journal": [],
                "sequence": 0,
            },
            "simulator": {
                "pending_orders": [_order("0.01")],
                "seen_order_ids": [str(ORDER_ID)],
                "last_bar_times": {"BTC-USDT-PERP": NOW.isoformat()},
            },
            "fills": [
                {
                    "order": _order("0.01"),
                    "fill": {
                        "order_id": str(ORDER_ID),
                        "symbol": "BTC-USDT-PERP",
                        "quantity": "0.01",
                        "price": "112100",
                        "commission": "0.75",
                        "timestamp": NOW.isoformat(),
                        "spread_bps_applied": "1",
                        "slippage_bps_applied": "1",
                        "impact_bps_applied": "0",
                    },
                }
            ],
        },
    }


def _shadow_checkpoint() -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_id": "shadow-main",
        "mode": "SHADOW",
        "status": "RUNNING",
        "halt_reason": None,
        "metrics": {
            "orders_received": 1,
            "paper_fills": 0,
            "shadow_decisions": 1,
            "reconciliations": 1,
            "reconciliation_failures": 0,
            "degradation_checks": 0,
            "degradation_pauses": 0,
            "errors": 0,
            "restarts": 0,
            "recovery_attempts": 0,
            "recovery_successes": 0,
            "heartbeats": 2,
        },
        "last_market_event_at": NOW.isoformat(),
        "last_reconcile_at": NOW.isoformat(),
        "last_degradation_at": None,
        "consecutive_failures": 0,
        "shadow": {
            "decisions": [
                {
                    "order": _order(),
                    "submission_result": {
                        "sent": False,
                        "client_order_id": str(ORDER_ID),
                        "venue_order_id": None,
                        "reason": "SHADOW_SUBMISSION_DISABLED",
                    },
                }
            ],
            "halted": False,
            "halt_reason": None,
        },
    }


def _seed_store(path: Path) -> Path:
    store = PersistentRuntimeStore(path)
    paper = store.claim("paper-main", "PAPER", "paper-owner", now=NOW, lease_seconds=30)
    store.set_status("paper-main", "paper-owner", status="RUNNING", halt_reason=None, now=NOW)
    store.append_event(
        "paper-main",
        "paper-owner",
        event_type="PAPER_BAR_PROCESSED",
        occurred_at=NOW,
        payload={"symbol": "BTC-USDT-PERP", "fills": 1, "ledger_reconciled": True},
    )
    store.append_event(
        "paper-main",
        "paper-owner",
        event_type="DEGRADATION_ASSESSMENT",
        occurred_at=NOW,
        payload={"state": "ACTIVE", "reasons": [], "metrics": {}},
    )
    store.save_checkpoint("paper-main", "paper-owner", generation=paper.generation, created_at=NOW, payload=_paper_checkpoint())

    shadow = store.claim("shadow-main", "SHADOW", "shadow-owner", now=NOW, lease_seconds=30)
    store.set_status("shadow-main", "shadow-owner", status="RUNNING", halt_reason=None, now=NOW)
    store.append_event(
        "shadow-main",
        "shadow-owner",
        event_type="SHADOW_RECONCILIATION",
        occurred_at=NOW,
        payload={"action": "CONTINUE", "reasons": [], "containment_action_sent": False},
    )
    store.save_checkpoint("shadow-main", "shadow-owner", generation=shadow.generation, created_at=NOW, payload=_shadow_checkpoint())
    return path


def _file_hash(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def test_runtime_reader_is_read_only_and_verifies_persisted_state(tmp_path: Path) -> None:
    db = _seed_store(tmp_path / "runtime.db")
    before = _file_hash(db)
    result = RuntimeStoreReadOnlyReader(db).snapshot(now=NOW + timedelta(seconds=1))
    after = _file_hash(db)

    assert before == after
    assert result["read_only"] is True
    assert result["runtime_count"] == 2
    assert {item["mode"] for item in result["runtimes"]} == {"PAPER", "SHADOW"}
    assert all(item["events_verified"] is True for item in result["runtimes"])
    assert all(item["checkpoint"] is not None for item in result["runtimes"])
    assert all(item["lease_state"] == "ACTIVE" for item in result["runtimes"])
    assert all("owner_id" not in item for item in result["runtimes"])


def test_live_adapter_replaces_demo_runtime_with_real_persisted_values(tmp_path: Path) -> None:
    db = _seed_store(tmp_path / "runtime.db")
    payload = LiveReadOnlyEqsAdapter(db, ROOT).snapshot(now=NOW + timedelta(seconds=1)).to_payload()

    assert payload["meta"]["access_mode"] == "READ_ONLY"
    assert payload["meta"]["adapter"] == "F5.6_RUNTIME_READ_ONLY_ADAPTER"
    assert payload["meta"]["mutations_enabled"] is False
    assert payload["execution"]["source"] == "LIVE_F5.6_RUNTIME_STORE"
    assert payload["execution"]["paper"]["cash"] == 99800.25
    assert payload["execution"]["paper"]["equity"] is None
    assert payload["execution"]["paper"]["unrealized_pnl"] is None
    assert payload["execution"]["paper"]["positions"][0]["qty"] == 0.01
    assert payload["execution"]["paper"]["reconciliation"] == "PASS"
    assert payload["execution"]["shadow"]["submitted_orders"] == 0
    assert payload["execution"]["shadow"]["zero_submit_invariant"] == "PASS_PERSISTED"
    assert payload["execution"]["shadow"]["venue_submission_enabled"] is None
    assert payload["risk"]["limits"] == []
    assert payload["risk"]["limits_state"] == "NOT_PERSISTED_IN_RUNTIME_STORE"
    assert payload["system_health"]["lease"]["state"] == "ACTIVE"


def test_corrupt_runtime_checkpoint_fails_closed_in_interface(tmp_path: Path) -> None:
    db = _seed_store(tmp_path / "runtime.db")
    with sqlite3.connect(db) as connection:
        connection.execute(
            "UPDATE runtime_checkpoints SET payload_sha256='bad' WHERE runtime_id='paper-main'"
        )
        connection.commit()

    payload = LiveReadOnlyEqsAdapter(db, ROOT).snapshot(now=NOW + timedelta(seconds=1)).to_payload()
    assert payload["meta"]["data_classification"] == "REAL_EVIDENCE_RUNTIME_STATUS_UNAVAILABLE"
    assert payload["execution"]["source"] == "RUNTIME_READ_FAILURE"
    assert payload["system_health"]["overall"] == "RUNTIME_STATUS_UNAVAILABLE"
    assert any(item["severity"] == "BLOCK" and "Runtime status unavailable" in item["message"] for item in payload["system_health"]["alerts"])


def test_runtime_status_endpoint_uses_same_get_only_contract(tmp_path: Path) -> None:
    db = _seed_store(tmp_path / "runtime.db")
    namespace = runpy.run_path(str(ROOT / "scripts/serve_readonly_interface.py"), run_name="eqs_interface_live_test_server")
    handler = namespace["ReadOnlyInterfaceHandler"]
    server_cls = namespace["ThreadingHTTPServer"]
    handler.adapter = LiveReadOnlyEqsAdapter(db, ROOT)
    server = server_cls(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        with urlopen(f"http://{host}:{port}/api/runtime/status", timeout=2) as response:
            body = json.loads(response.read())
            assert response.status == 200
            assert body["meta"]["mutations_enabled"] is False
            assert body["execution"]["source"] == "LIVE_F5.6_RUNTIME_STORE"
            assert body["execution"]["shadow"]["submitted_orders"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
