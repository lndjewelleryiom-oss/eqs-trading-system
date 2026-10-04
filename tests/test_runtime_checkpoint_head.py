from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3

import pytest

from quant_system.runtime.store import CheckpointCorruptionError, PersistentRuntimeStore


NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


def payload(runtime_id: str, value: int, *, status: str = "RUNNING") -> dict[str, object]:
    return {
        "schema_version": 1,
        "runtime_id": runtime_id,
        "mode": "PAPER",
        "status": status,
        "halt_reason": None,
        "metrics": {"heartbeats": value},
        "last_market_event_at": None,
        "last_reconcile_at": None,
        "last_degradation_at": None,
        "consecutive_failures": 0,
        "paper_valuations": [{"i": i, "value": "x" * 500} for i in range(50)],
        "paper": {
            "ledger": {
                "initial_cash": "10000",
                "cash": "10000",
                "positions": {},
                "commissions": "0",
                "financing_costs": "0",
                "borrow_costs": "0",
                "dividend_cashflow": "0",
                "journal": [],
                "sequence": 0,
            },
            "simulator": {"pending_orders": [], "seen_order_ids": [], "last_bar_times": {}},
            "fills": [],
        },
    }


def counts(path: Path) -> tuple[int, int]:
    with closing(sqlite3.connect(path)) as con:
        history = con.execute("SELECT COUNT(*) FROM runtime_checkpoints").fetchone()[0]
        heads = con.execute("SELECT COUNT(*) FROM runtime_checkpoint_heads").fetchone()[0]
    return int(history), int(heads)


def test_routine_checkpoints_update_one_head_without_history_growth(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    lease = store.claim("paper-main", "PAPER", "owner", now=NOW, lease_seconds=30)

    for i in range(100):
        store.save_checkpoint(
            "paper-main",
            "owner",
            generation=lease.generation,
            created_at=NOW + timedelta(milliseconds=i),
            payload=payload("paper-main", i),
        )

    assert counts(path) == (1, 1)
    latest = store.latest_checkpoint("paper-main")
    assert latest is not None
    assert latest.payload["metrics"]["heartbeats"] == 99


def test_terminal_state_is_archived_without_deleting_prior_history(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    lease = store.claim("paper-main", "PAPER", "owner", now=NOW, lease_seconds=30)
    for i in range(10):
        store.save_checkpoint(
            "paper-main",
            "owner",
            generation=lease.generation,
            created_at=NOW + timedelta(seconds=i),
            payload=payload("paper-main", i),
        )
    store.save_checkpoint(
        "paper-main",
        "owner",
        generation=lease.generation,
        created_at=NOW + timedelta(seconds=20),
        payload=payload("paper-main", 20, status="STOPPED"),
    )
    assert counts(path) == (2, 1)

    with closing(sqlite3.connect(path)) as con:
        statuses = [
            row[0]
            for row in con.execute(
                "SELECT json_extract(payload_json,'$.status') FROM runtime_checkpoints ORDER BY id"
            )
        ]
    assert statuses == ["RUNNING", "STOPPED"]


def test_new_generation_gets_new_sparse_archive(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    first = store.claim("paper-main", "PAPER", "owner-a", now=NOW, lease_seconds=30)
    store.save_checkpoint(
        "paper-main", "owner-a", generation=first.generation, created_at=NOW,
        payload=payload("paper-main", 1),
    )
    store.release("paper-main", "owner-a", now=NOW + timedelta(seconds=1))
    second = store.claim(
        "paper-main", "PAPER", "owner-b", now=NOW + timedelta(seconds=2), lease_seconds=30
    )
    assert second.generation > first.generation
    store.save_checkpoint(
        "paper-main", "owner-b", generation=second.generation,
        created_at=NOW + timedelta(seconds=2), payload=payload("paper-main", 2),
    )
    assert counts(path) == (2, 1)


def test_matching_archived_checkpoint_corruption_still_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    lease = store.claim("paper-main", "PAPER", "owner", now=NOW, lease_seconds=30)
    store.save_checkpoint(
        "paper-main", "owner", generation=lease.generation, created_at=NOW,
        payload=payload("paper-main", 1),
    )
    with closing(sqlite3.connect(path)) as con:
        with con:
            con.execute("UPDATE runtime_checkpoints SET payload_json='{}'")

    with pytest.raises(CheckpointCorruptionError, match="archived runtime checkpoint"):
        store.latest_checkpoint("paper-main")


def test_legacy_history_without_head_remains_recoverable(tmp_path: Path) -> None:
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    lease = store.claim("paper-main", "PAPER", "owner", now=NOW, lease_seconds=30)
    store.save_checkpoint(
        "paper-main", "owner", generation=lease.generation, created_at=NOW,
        payload=payload("paper-main", 1),
    )
    with closing(sqlite3.connect(path)) as con:
        with con:
            con.execute("DELETE FROM runtime_checkpoint_heads")

    latest = store.latest_checkpoint("paper-main")
    assert latest is not None
    assert latest.payload["metrics"]["heartbeats"] == 1
