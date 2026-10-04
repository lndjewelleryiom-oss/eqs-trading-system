from pathlib import Path
import json
import sqlite3

from quant_system.performance.autonomy import PerformanceAutonomyCycle
from quant_system.performance.ledger import _payload_hash


def _runtime(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
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
    event_id = 1
    for runtime_id in ("current-bybit_linear-paper", "current-okx_swap-paper"):
        payload = {
            "cash":"10000","equity":"10000","realized_pnl":"0","unrealized_pnl":"0",
            "commissions":"0","financing_costs":"0",
            "observed_at":"2026-10-04T00:00:00+00:00",
            "source":{"classification":"GENUINE_PUBLIC_MARKET","venue":"X"},
        }
        con.execute(
            "INSERT INTO runtime_events VALUES(?,?,?,?,?,?,?)",
            (
                event_id,runtime_id,1,"PAPER_VALUATION",
                "2026-10-04T00:00:00+00:00",
                json.dumps(payload,sort_keys=True,separators=(",",":")),
                _payload_hash(payload),
            ),
        )
        event_id += 1
    con.commit()
    con.close()


def test_cycle_is_paper_only_and_waits_for_genuine_strategy(tmp_path):
    root = tmp_path / "app"
    runtime = root / "artifacts" / "commissioning" / "current_runtime_v1" / "runtime.db"
    _runtime(runtime)
    state = PerformanceAutonomyCycle(root).run()
    assert state["mode"] == "PAPER_ONLY"
    assert state["live_authority"] is False
    assert state["broker_submission_enabled"] is False
    assert state["status"] == "READY_WAITING_FOR_GENUINE_STRATEGY"
    assert state["performance_autonomy_ready"] is False
    assert any(b["reason"] == "NO_GENUINE_VALIDATED_STRATEGY_AVAILABLE" for b in state["blockers"])
