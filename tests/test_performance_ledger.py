import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from quant_system.performance.ledger import PerformanceLedger, _payload_hash


def _runtime_db(path: Path) -> Path:
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE runtime_events(
            id INTEGER PRIMARY KEY,
            runtime_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL
        );
        """
    )
    con.commit()
    con.close()
    return path


def _event(con, *, event_id, runtime_id, sequence, event_type, occurred_at, payload):
    con.execute(
        """INSERT INTO runtime_events(
           id,runtime_id,sequence,event_type,occurred_at,payload_json,payload_sha256
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            event_id,
            runtime_id,
            sequence,
            event_type,
            occurred_at,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            _payload_hash(payload),
        ),
    )


def test_ingest_is_idempotent_and_excludes_infrastructure_strategy(tmp_path):
    runtime = _runtime_db(tmp_path / "runtime.db")
    con = sqlite3.connect(runtime)
    _event(
        con,
        event_id=1,
        runtime_id="current-bybit_linear-paper",
        sequence=1,
        event_type="STRATEGY_DECISION",
        occurred_at="2026-10-04T00:00:00+00:00",
        payload={
            "strategy_id": "infra-id",
            "strategy_version": "deterministic-infrastructure-test-v1",
            "signal": "SELL",
            "reason_codes": ["DETERMINISTIC_TEST_SIGNAL_ONLY"],
        },
    )
    for event_id, equity in ((2, "10000"), (3, "9900"), (4, "10100")):
        _event(
            con,
            event_id=event_id,
            runtime_id="current-bybit_linear-paper",
            sequence=event_id,
            event_type="PAPER_VALUATION",
            occurred_at=f"2026-10-04T00:0{event_id}:00+00:00",
            payload={
                "cash": equity,
                "equity": equity,
                "realized_pnl": "0",
                "unrealized_pnl": "0",
                "commissions": "0",
                "financing_costs": "0",
                "observed_at": f"2026-10-04T00:0{event_id}:00+00:00",
                "source": {"classification": "GENUINE_PUBLIC_MARKET", "venue": "BYBIT_LINEAR"},
            },
        )
    con.commit()
    con.close()

    ledger = PerformanceLedger(tmp_path / "performance.db")
    first = ledger.ingest_runtime_events(runtime)
    second = ledger.ingest_runtime_events(runtime)

    assert first == {"events_seen": 4, "valuations_added": 3, "strategy_events_added": 1}
    assert second == {"events_seen": 0, "valuations_added": 0, "strategy_events_added": 0}
    activity = ledger.strategy_activity()
    assert len(activity) == 1
    assert activity[0].performance_claim_eligible is False
    assert "INFRASTRUCTURE_TEST_STRATEGY" in activity[0].exclusion_reason

    snap = ledger.runtime_snapshot("current-bybit_linear-paper")
    assert snap.observations == 3
    assert snap.starting_equity == Decimal("10000")
    assert snap.ending_equity == Decimal("10100")
    assert snap.return_fraction == Decimal("0.01")
    assert snap.max_drawdown_fraction == Decimal("0.01")
    assert snap.performance_claim_eligible is False
    assert "NO_ELIGIBLE_STRATEGY_ACTIVITY" in snap.exclusion_reasons
    assert ledger.verify_hash_chain()


def test_genuine_non_test_strategy_marks_runtime_claim_eligible(tmp_path):
    runtime = _runtime_db(tmp_path / "runtime.db")
    con = sqlite3.connect(runtime)
    _event(
        con,
        event_id=1,
        runtime_id="current-okx_swap-paper",
        sequence=1,
        event_type="STRATEGY_DECISION",
        occurred_at="2026-10-04T00:00:00+00:00",
        payload={
            "strategy_id": "candidate-1",
            "strategy_version": "paper-canary-v1",
            "signal": "BUY",
            "reason_codes": ["MODEL_SIGNAL"],
        },
    )
    _event(
        con,
        event_id=2,
        runtime_id="current-okx_swap-paper",
        sequence=2,
        event_type="PAPER_VALUATION",
        occurred_at="2026-10-04T00:01:00+00:00",
        payload={
            "cash": "10000",
            "equity": "10000",
            "realized_pnl": "0",
            "unrealized_pnl": "0",
            "commissions": "0",
            "financing_costs": "0",
            "observed_at": "2026-10-04T00:01:00+00:00",
            "source": {"classification": "GENUINE_PUBLIC_MARKET", "venue": "OKX_SWAP"},
        },
    )
    con.commit()
    con.close()

    ledger = PerformanceLedger(tmp_path / "performance.db")
    ledger.ingest_runtime_events(runtime)
    snap = ledger.runtime_snapshot("current-okx_swap-paper")
    assert snap.performance_claim_eligible is True


def test_payload_hash_mismatch_fails_closed(tmp_path):
    runtime = _runtime_db(tmp_path / "runtime.db")
    con = sqlite3.connect(runtime)
    payload = {
        "cash": "10000",
        "equity": "10000",
        "realized_pnl": "0",
        "unrealized_pnl": "0",
        "commissions": "0",
        "financing_costs": "0",
        "observed_at": "2026-10-04T00:01:00+00:00",
    }
    con.execute(
        """INSERT INTO runtime_events(
           id,runtime_id,sequence,event_type,occurred_at,payload_json,payload_sha256
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            1,
            "current-bybit_linear-paper",
            1,
            "PAPER_VALUATION",
            "2026-10-04T00:01:00+00:00",
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            "0" * 64,
        ),
    )
    con.commit()
    con.close()

    ledger = PerformanceLedger(tmp_path / "performance.db")
    with pytest.raises(ValueError, match="payload hash mismatch"):
        ledger.ingest_runtime_events(runtime)
