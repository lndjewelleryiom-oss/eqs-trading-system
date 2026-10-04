from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


class StrategyRunReadError(RuntimeError):
    pass


class StrategyRunReadOnlyReader:
    REQUIRED_TABLES = {"strategy_runs", "strategy_run_events"}

    def __init__(self, path: str | Path):
        self.path = Path(path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise StrategyRunReadError("strategy run registry does not exist")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True, timeout=2.0)
        except sqlite3.Error as exc:
            raise StrategyRunReadError(f"cannot open strategy registry read-only: {type(exc).__name__}") from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            present = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = self.REQUIRED_TABLES - present
            if missing:
                raise StrategyRunReadError("strategy registry schema incomplete")
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["parameters"] = json.loads(out.pop("parameter_json"))
        out["broker_submission_enabled"] = bool(out["broker_submission_enabled"])
        out["live_authority"] = bool(out["live_authority"])
        return out

    @staticmethod
    def _event(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": "EQS-STRATEGY-RUN-EVENT-V1",
            "event_id": row["event_id"],
            "sequence": int(row["sequence"]),
            "run_id": row["run_id"],
            "event_type": row["event_type"],
            "market_time_utc": row["market_time_utc"],
            "received_time_utc": row["received_time_utc"],
            "emitted_time_utc": row["emitted_time_utc"],
            "payload": json.loads(row["payload_json"]),
            "previous_event_hash": row["previous_event_hash"],
            "event_hash": row["event_hash"],
        }

    def list_runs(self, *, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as c:
            rows = c.execute(
                "SELECT * FROM strategy_runs ORDER BY created_at DESC, run_id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._run(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as c:
            row = c.execute("SELECT * FROM strategy_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return self._run(row)

    def events(self, run_id: str, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 5000))
        with self._connect() as c:
            rows = c.execute(
                """
                SELECT * FROM strategy_run_events
                WHERE run_id=? AND sequence>?
                ORDER BY sequence ASC LIMIT ?
                """,
                (run_id, max(0, int(after)), limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def event_projection(self, run_id: str) -> dict[str, Any]:
        events = self.events(run_id, after=0, limit=5000)
        fills = []
        trades = []
        equity = []
        bars = []
        signals = []
        orders = []
        diagnostics = []
        for event in events:
            kind = event["event_type"]
            row = {
                "sequence": event["sequence"],
                "market_time_utc": event["market_time_utc"],
                **event["payload"],
            }
            if kind in {"FILL", "PARTIAL_FILL"}:
                fills.append(row)
            elif kind == "TRADE_CLOSED":
                trades.append(row)
            elif kind == "EQUITY_UPDATE":
                equity.append(row)
            elif kind in {"MARKET_BAR", "MARKET_TICK"}:
                bars.append(row)
            elif kind == "SIGNAL":
                signals.append(row)
            elif kind in {"ORDER_SUBMITTED", "ORDER_ACK", "ORDER_REJECT"}:
                orders.append({"event_type": kind, **row})
            elif kind in {"RUN_BLOCKED", "RUN_FAILED", "RUN_PAUSED"}:
                diagnostics.append({"event_type": kind, **row})
        return {
            "run": self.get_run(run_id),
            "events": events,
            "fills": fills,
            "trades": trades,
            "equity": equity,
            "market": bars,
            "signals": signals,
            "orders": orders,
            "diagnostics": diagnostics,
        }
