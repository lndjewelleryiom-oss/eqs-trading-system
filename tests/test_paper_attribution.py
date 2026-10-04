import json
import sqlite3
from hashlib import sha256

import pytest

from quant_system.performance.attribution import (
    AttributionError,
    PersistentPaperAttributionLedger,
)
from quant_system.performance.ledger import INFRASTRUCTURE_TEST_STRATEGY_VERSION


def _canonical(payload):
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _sha(payload):
    return sha256(_canonical(payload)).hexdigest()


def _runtime_db(path, *, strategy_version="paper-alpha-v1", corrupt_event=False):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE execution_v1_events(
            id INTEGER PRIMARY KEY,
            runtime_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_id TEXT NOT NULL,
            execution_intent_id TEXT NOT NULL,
            client_order_id TEXT NOT NULL,
            runtime_mode TEXT NOT NULL,
            truth_source TEXT NOT NULL,
            authoritative_external_truth INTEGER NOT NULL,
            occurred_at TEXT NOT NULL,
            event_json TEXT NOT NULL,
            event_sha256 TEXT NOT NULL
        );
        CREATE TABLE execution_v1_bindings(
            runtime_id TEXT NOT NULL,
            client_order_id TEXT NOT NULL,
            execution_intent_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            intent_json TEXT NOT NULL,
            intent_sha256 TEXT NOT NULL,
            capability_json TEXT NOT NULL,
            capability_sha256 TEXT NOT NULL,
            interlock_json TEXT NOT NULL,
            interlock_sha256 TEXT NOT NULL,
            authority_json TEXT NOT NULL,
            authority_sha256 TEXT NOT NULL,
            PRIMARY KEY(runtime_id, client_order_id)
        );
        """
    )
    intent = {
        "strategy": {
            "strategy_id": "alpha-1",
            "strategy_version": strategy_version,
            "campaign_id": "campaign-1",
        },
        "route": {"venue_id": "BYBIT_LINEAR"},
    }
    event = {
        "event_id": "fill-event-0001",
        "execution_id": "exec-intent-0001",
        "client_order_id": "client-order-0001",
        "venue_id": "BYBIT_LINEAR",
        "event_type": "FILL",
        "receive_timestamp": "2026-10-04T12:00:00+00:00",
        "payload_hash": "1" * 64,
        "fill": {
            "fill_id": "fill-0001",
            "quantity": "0.01",
            "price": "85000",
            "liquidity_role": "TAKER",
        },
        "fees": [
            {"fee_type": "COMMISSION", "amount": "0.85", "currency": "USDT"}
        ],
        "schema_version": "EQS-EXEC-TRUTH-EVENT-v1.0",
    }
    con.execute(
        """INSERT INTO execution_v1_bindings(
           runtime_id,client_order_id,execution_intent_id,mode,recorded_at,
           intent_json,intent_sha256,capability_json,capability_sha256,
           interlock_json,interlock_sha256,authority_json,authority_sha256
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            "paper-alpha-runtime",
            "client-order-0001",
            "exec-intent-0001",
            "PAPER",
            "2026-10-04T11:59:59+00:00",
            _canonical(intent).decode(),
            _sha(intent),
            "{}","0"*64,"{}","0"*64,"{}","0"*64,
        ),
    )
    con.execute(
        """INSERT INTO execution_v1_events(
           id,runtime_id,sequence,event_id,execution_intent_id,client_order_id,
           runtime_mode,truth_source,authoritative_external_truth,occurred_at,
           event_json,event_sha256
           ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            1,
            "paper-alpha-runtime",
            1,
            "fill-event-0001",
            "exec-intent-0001",
            "client-order-0001",
            "PAPER",
            "SIMULATOR",
            0,
            "2026-10-04T12:00:00+00:00",
            _canonical(event).decode(),
            ("f"*64 if corrupt_event else _sha(event)),
        ),
    )
    con.commit()
    con.close()
    return path


def test_canonical_paper_fill_is_attributed_to_strategy(tmp_path):
    runtime = _runtime_db(tmp_path / "runtime.db")
    ledger = PersistentPaperAttributionLedger(tmp_path / "attrib.db")
    first = ledger.ingest_runtime_execution(runtime)
    second = ledger.ingest_runtime_execution(runtime)

    assert first["paper_fill_rows_added"] == 1
    assert first["eligible_fill_rows_added"] == 1
    assert second["paper_fill_rows_added"] == 0
    rows = ledger.fills(eligible_only=True)
    assert len(rows) == 1
    assert rows[0].strategy_id == "alpha-1"
    assert rows[0].strategy_version == "paper-alpha-v1"
    assert str(rows[0].quantity) == "0.01"
    assert str(rows[0].price) == "85000"
    assert str(rows[0].fee_amount) == "0.85"
    assert ledger.strategy_counts() == {("alpha-1", "paper-alpha-v1"): 1}
    assert ledger.verify_hash_chain()


def test_infrastructure_strategy_fill_is_not_performance_eligible(tmp_path):
    runtime = _runtime_db(
        tmp_path / "runtime.db",
        strategy_version=INFRASTRUCTURE_TEST_STRATEGY_VERSION,
    )
    ledger = PersistentPaperAttributionLedger(tmp_path / "attrib.db")
    result = ledger.ingest_runtime_execution(runtime)
    assert result["paper_fill_rows_added"] == 1
    assert result["eligible_fill_rows_added"] == 0
    row = ledger.fills()[0]
    assert row.performance_claim_eligible is False
    assert row.exclusion_reason == "INFRASTRUCTURE_TEST_STRATEGY"


def test_corrupt_execution_event_fails_closed(tmp_path):
    runtime = _runtime_db(tmp_path / "runtime.db", corrupt_event=True)
    ledger = PersistentPaperAttributionLedger(tmp_path / "attrib.db")
    with pytest.raises(AttributionError, match="EXECUTION_EVENT_HASH_INVALID"):
        ledger.ingest_runtime_execution(runtime)
