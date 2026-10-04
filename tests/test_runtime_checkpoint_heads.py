from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
import sqlite3

import pytest

from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import CheckpointCorruptionError, PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode


def paper_engine() -> PaperTradingEngine:
    assumptions = ExecutionAssumptions(
        commission_bps=Decimal("1"),
        spread_bps=Decimal("2"),
        slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"),
        financing_bps_annual=Decimal("0"),
        borrow_bps_annual=Decimal("0"),
        latency_ms=0,
    )
    return PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions), initial_cash=Decimal("10000"))


def counts(path):
    with closing(sqlite3.connect(path)) as con:
        head = con.execute("SELECT COUNT(*) FROM runtime_checkpoint_heads").fetchone()[0]
        archive = con.execute("SELECT COUNT(*) FROM runtime_checkpoints").fetchone()[0]
    return head, archive


def test_repeated_heartbeats_overwrite_one_head_without_archive_growth(tmp_path):
    path = tmp_path / "runtime.db"
    runtime = PersistentPaperShadowRuntime(
        runtime_id="bounded-paper",
        mode=RuntimeMode.PAPER,
        store=PersistentRuntimeStore(path),
        paper_engine=paper_engine(),
    )
    runtime.start()
    assert counts(path) == (1, 1)

    for _ in range(25):
        runtime.heartbeat()

    assert counts(path) == (1, 1)
    runtime.stop()
    assert counts(path) == (1, 2)


def test_restart_uses_head_and_starts_new_sparse_generation_archive(tmp_path):
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    first = PersistentPaperShadowRuntime(
        runtime_id="bounded-paper",
        mode=RuntimeMode.PAPER,
        store=store,
        paper_engine=paper_engine(),
    )
    first.start()
    first.heartbeat()
    first.stop()
    assert counts(path) == (1, 2)

    resumed = PersistentPaperShadowRuntime(
        runtime_id="bounded-paper",
        mode=RuntimeMode.PAPER,
        store=store,
        paper_engine=paper_engine(),
    )
    resumed.start()
    assert resumed.health().metrics["restarts"] == 1
    assert counts(path) == (1, 3)
    for _ in range(10):
        resumed.heartbeat()
    assert counts(path) == (1, 3)


def test_corrupt_checkpoint_head_fails_closed(tmp_path):
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    first = PersistentPaperShadowRuntime(
        runtime_id="bounded-paper",
        mode=RuntimeMode.PAPER,
        store=store,
        paper_engine=paper_engine(),
    )
    first.start()
    first.stop()

    with closing(sqlite3.connect(path)) as con:
        with con:
            con.execute("UPDATE runtime_checkpoint_heads SET payload_json='{}' WHERE runtime_id='bounded-paper'")

    resumed = PersistentPaperShadowRuntime(
        runtime_id="bounded-paper",
        mode=RuntimeMode.PAPER,
        store=store,
        paper_engine=paper_engine(),
    )
    with pytest.raises(CheckpointCorruptionError):
        resumed.start()
    record = store.get_runtime("bounded-paper")
    assert record is not None
    assert record.status == "HALTED"
    assert record.halt_reason == "STARTUP_RECOVERY_FAILURE:CheckpointCorruptionError"
