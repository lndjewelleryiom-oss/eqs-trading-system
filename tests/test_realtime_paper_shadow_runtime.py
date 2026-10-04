from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from uuid import uuid4

import pytest

from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter, VenueOrder, VenueOrderStatus
from quant_system.execution.models import OrderRequest
from quant_system.monitoring import ExpectedBehavior, ObservedBehavior
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import (
    CheckpointCorruptionError,
    PersistentPaperShadowRuntime,
    PersistentRuntimeStore,
    RuntimeConfig,
    RuntimeLeaseError,
    RuntimeMode,
    RuntimeStatus,
)
from quant_system.shadow import ShadowExecutionEngine


START = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)
EXPECTED = ExpectedBehavior(0.55, 0.002, 1.5, 0.10, 5.0, 100)


def assumptions() -> ExecutionAssumptions:
    return ExecutionAssumptions(
        commission_bps=Decimal("1"),
        spread_bps=Decimal("2"),
        slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"),
        financing_bps_annual=Decimal("0"),
        borrow_bps_annual=Decimal("0"),
        latency_ms=0,
    )


def paper_engine() -> PaperTradingEngine:
    return PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions()), initial_cash=Decimal("10000"))


def make_order(quantity: str = "10") -> OrderRequest:
    return OrderRequest(
        uuid4(),
        "BTC-PERP",
        Side.BUY,
        Decimal(quantity),
        OrderType.MARKET,
        START,
        Decimal("100"),
    )


def bar(minutes: int, volume: str) -> BarEvent:
    return BarEvent(
        "BTC-PERP",
        START + timedelta(minutes=minutes),
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        Decimal(volume),
    )


def test_paper_restart_restores_ledger_pending_orders_and_observability(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    order = make_order()
    first = PersistentPaperShadowRuntime(runtime_id="paper-a", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    first.start()
    first.submit_order(order)
    first.on_bar(bar(1, "100"))  # participation cap fills 5/10 and leaves 5 pending
    before = first.paper_engine.snapshot({"BTC-PERP": Decimal("100")})
    assert before.positions["BTC-PERP"] == Decimal("5")
    assert first.paper_engine.simulator.pending_count == 1
    first.stop()

    resumed = PersistentPaperShadowRuntime(runtime_id="paper-a", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    health = resumed.start()
    assert health.metrics["restarts"] == 1
    assert resumed.paper_engine.simulator.pending_count == 1
    restored = resumed.paper_engine.snapshot({"BTC-PERP": Decimal("100")})
    assert restored == before

    resumed.on_bar(bar(2, "1000"))
    final = resumed.paper_engine.snapshot({"BTC-PERP": Decimal("100")})
    assert final.positions["BTC-PERP"] == Decimal("10")
    assert resumed.paper_engine.simulator.pending_count == 0
    assert resumed.paper_engine.ledger.reconcile().ok
    assert resumed.health().metrics["paper_fills"] == 2
    assert store.verify_events("paper-a") > 0


def test_restart_result_matches_uninterrupted_execution(tmp_path):
    order = make_order()
    control = paper_engine()
    control.submit(order)
    control.on_bar(bar(1, "100"))
    control.on_bar(bar(2, "1000"))
    expected = control.snapshot({"BTC-PERP": Decimal("100")})

    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    first = PersistentPaperShadowRuntime(runtime_id="paper-b", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    first.start()
    first.submit_order(order)
    first.on_bar(bar(1, "100"))
    first.stop()

    resumed = PersistentPaperShadowRuntime(runtime_id="paper-b", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    resumed.start()
    resumed.on_bar(bar(2, "1000"))
    assert resumed.paper_engine.snapshot({"BTC-PERP": Decimal("100")}) == expected
    assert resumed.paper_engine.fills == control.fills


def test_shadow_rechecks_submission_disable_before_every_order(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=False)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shadow-a",
        mode=RuntimeMode.SHADOW,
        store=store,
        shadow_engine=ShadowExecutionEngine(gateway),
    )
    runtime.start()
    gateway.venue_submission_enabled = True  # simulate unsafe external mutation
    with pytest.raises(RuntimeError, match="submission enabled"):
        runtime.submit_order(make_order("1"))
    assert adapter.submitted == []
    assert runtime.status == RuntimeStatus.HALTED


def test_shadow_decision_is_persisted_without_submission(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shadow-b",
        mode=RuntimeMode.SHADOW,
        store=store,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
    )
    runtime.start()
    decision = runtime.submit_order(make_order("1"))
    assert not decision.submission_result.sent
    assert decision.submission_result.reason == "VENUE_SUBMISSION_DISABLED"
    assert adapter.submitted == []
    assert runtime.health().metrics["shadow_decisions"] == 1


def test_shadow_reconciliation_halts_without_sending_containment_action(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    unknown = uuid4()
    adapter.open_orders[unknown] = VenueOrder(unknown, "V-X", "BTC-PERP", VenueOrderStatus.ACKNOWLEDGED, Decimal("1"))
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shadow-c",
        mode=RuntimeMode.SHADOW,
        store=store,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
    )
    runtime.start()
    result = runtime.reconcile_shadow(expected_open_order_ids=set(), expected_positions={})
    assert result.action.value == "HALT"
    assert runtime.status == RuntimeStatus.HALTED
    assert adapter.canceled == []
    assert adapter.submitted == []


def test_degradation_pause_is_persisted_and_blocks_more_work(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="paper-c", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    runtime.start()
    assessment = runtime.assess_degradation(
        EXPECTED,
        ObservedBehavior(0.30, -0.001, 0.2, 0.15, 12.0, 30),
    )
    assert assessment.state.value == "PAUSED"
    assert runtime.status == RuntimeStatus.HALTED
    with pytest.raises(RuntimeError, match="not operational"):
        runtime.submit_order(make_order("1"))
    assert store.get_runtime("paper-c").status == "HALTED"


def test_runtime_lease_prevents_concurrent_orchestrators(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    first = PersistentPaperShadowRuntime(runtime_id="paper-d", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    second = PersistentPaperShadowRuntime(runtime_id="paper-d", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    first.start()
    with pytest.raises(RuntimeLeaseError):
        second.start()
    first.stop()
    second.start()
    assert second.status == RuntimeStatus.RUNNING


def test_corrupt_checkpoint_fails_closed_on_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    first = PersistentPaperShadowRuntime(runtime_id="paper-e", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    first.start()
    first.stop()

    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("UPDATE runtime_checkpoints SET payload_json='{}' WHERE id=(SELECT MAX(id) FROM runtime_checkpoints)")

    resumed = PersistentPaperShadowRuntime(runtime_id="paper-e", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    with pytest.raises(CheckpointCorruptionError):
        resumed.start()
    record = store.get_runtime("paper-e")
    assert record.status == "HALTED"
    assert record.halt_reason == "STARTUP_RECOVERY_FAILURE:CheckpointCorruptionError"


def test_recovery_requires_clean_state_and_records_success(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="paper-f", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine())
    runtime.start()
    runtime.halt("INJECTED_TEST_FAILURE")
    assert runtime.attempt_recovery()
    assert runtime.status == RuntimeStatus.RUNNING
    health = runtime.health()
    assert health.metrics["recovery_attempts"] == 1
    assert health.metrics["recovery_successes"] == 1


def test_transient_failure_threshold_degrades_then_halts_and_recovers(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-g",
        mode=RuntimeMode.PAPER,
        store=store,
        paper_engine=paper_engine(),
        config=RuntimeConfig(max_consecutive_failures=2),
    )
    runtime.start()
    assert runtime.report_failure("MARKET_DATA", ConnectionError("injected")) == RuntimeStatus.DEGRADED
    assert runtime.report_failure("MARKET_DATA", ConnectionError("injected")) == RuntimeStatus.HALTED
    assert runtime.health().metrics["errors"] == 2
    assert runtime.attempt_recovery()
    assert runtime.status == RuntimeStatus.RUNNING
