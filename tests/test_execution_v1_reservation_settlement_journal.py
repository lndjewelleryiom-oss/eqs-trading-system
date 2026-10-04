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
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest
from quant_system.execution.v1 import (
    AssetClass,
    Capability,
    ConnectionCapability,
    ConstraintSnapshot,
    CryptoBrokerAdapterBridge,
    CryptoOrderIntentContext,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionMode,
    InterlockIssuer,
    InterlockState,
    SubmissionInterlock,
    TimeInForce,
    order_request_to_order_intent,
)
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import CheckpointCorruptionError, PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode
from quant_system.shadow import ShadowExecutionEngine


NOW = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
SEAL = "c" * 64


def assumptions() -> ExecutionAssumptions:
    return ExecutionAssumptions(
        commission_bps=Decimal("1"), spread_bps=Decimal("2"), slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"), financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("0"), latency_ms=0,
    )


def paper_engine() -> PaperTradingEngine:
    return PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions()), initial_cash=Decimal("10000"))


def order(quantity: str = "10") -> OrderRequest:
    return OrderRequest(
        strategy_id=uuid4(), symbol="BTC-PERP", side=Side.BUY, quantity=Decimal(quantity),
        order_type=OrderType.MARKET, decision_time=NOW, reference_price=Decimal("100"),
    )


def context(mode: ExecutionMode, capability: Capability) -> CryptoOrderIntentContext:
    return CryptoOrderIntentContext(
        portfolio_decision_id="portfolio-decision-001", portfolio_id="crypto-portfolio",
        reservation_id="reservation-001", risk_authorisation_id="risk-auth-001",
        authorised_at=NOW - timedelta(seconds=10), expires_at=NOW + timedelta(minutes=10),
        venue_id="CRYPTO-FIXTURE", connection_id="crypto-fixture-main", required_capability=capability,
        mode=mode, programme_state_ref="programme-state-001", interlock_ref="interlock-001",
        market_state_ref="market-state-001", reference_data_ref="reference-data-001",
        instrument_id="CRYPTO:PERP:BTC", currency="USD", strategy_version="legacy-crypto-v1",
        time_in_force=TimeInForce.GTC, asset_extension={"product_type": "PERPETUAL"},
    )


def capability(required: Capability) -> ConnectionCapability:
    return ConnectionCapability(
        connection_id="crypto-fixture-main", venue_id="CRYPTO-FIXTURE", capabilities=(required,),
        verified_at=NOW - timedelta(seconds=5), verification_ref="capability-evidence-001",
        production_submission_enabled=False,
    )


def interlock(state: InterlockState) -> SubmissionInterlock:
    return SubmissionInterlock(
        interlock_id="interlock-001", revision=1, state=state, issued_by=InterlockIssuer.EQS_00,
        valid_from=NOW - timedelta(minutes=1), valid_until=NOW + timedelta(minutes=30),
        programme_state_ref="programme-state-001", seal_hash=SEAL,
    )


def canonical_adapter(required: Capability, adapter=None, *, state_status: str = "TRADING") -> CryptoBrokerAdapterBridge:
    adapter = adapter or InMemoryBrokerAdapter()
    cap = capability(required)
    snapshot = ConstraintSnapshot(
        snapshot_id="constraint-fixture-001", asset_class=AssetClass.CRYPTO,
        instrument_id="CRYPTO:PERP:BTC", venue_id="CRYPTO-FIXTURE", venue_symbol="BTC-PERP",
        observed_at=NOW - timedelta(seconds=1), valid_until=NOW + timedelta(hours=1),
        source_ref="fixture-reference-data-001", source_version="fixture-v1", status=state_status,
        tick_size=Decimal("1"), lot_size=Decimal("1"), asset_extension={"product_type": "PERPETUAL"},
    )
    return CryptoBrokerAdapterBridge(
        adapter, connection_capability=cap, constraint_snapshots={snapshot.instrument_id: snapshot},
        symbol_to_instrument_id={"BTC-PERP": snapshot.instrument_id},
    )


def bar(minutes: int, volume: str) -> BarEvent:
    return BarEvent(
        "BTC-PERP", NOW + timedelta(minutes=minutes), Decimal("100"), Decimal("101"), Decimal("99"),
        Decimal("100"), Decimal(volume),
    )


def bind_paper(runtime: PersistentPaperShadowRuntime, legacy: OrderRequest):
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    runtime.submit_v1_order(
        legacy, intent=intent, capability=capability(Capability.PUBLIC_ONLY),
        interlock=interlock(InterlockState.PAPER_ONLY),
        venue_adapter=canonical_adapter(Capability.PUBLIC_ONLY),
    )
    return intent


def bind_shadow(runtime: PersistentPaperShadowRuntime, legacy: OrderRequest):
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.SHADOW, Capability.PRIVATE_READ_ONLY))
    runtime.submit_v1_order(
        legacy, intent=intent, capability=capability(Capability.PRIVATE_READ_ONLY),
        interlock=interlock(InterlockState.SHADOW_ZERO_SUBMIT),
        venue_adapter=canonical_adapter(Capability.PRIVATE_READ_ONLY, runtime.shadow_engine.gateway.adapter),
    )
    return intent


def test_paper_partial_and_final_fills_atomically_emit_reservation_settlements(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-settle", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order("10")
    intent = bind_paper(runtime, legacy)

    first = runtime.on_bar(bar(1, "100"))
    assert first[0].fill.quantity == Decimal("5")
    settlements = store.load_v1_reservation_settlements("paper-settle")
    assert len(settlements) == 1
    partial = settlements[0]
    assert partial.reservation_id == "reservation-001"
    assert partial.execution_id == intent.execution_intent_id
    assert partial.settlement["event"] == "PARTIAL_FILL"
    assert partial.settlement["filled_quantity"] == "5.00"
    assert partial.settlement["remaining_quantity"] == "5.00"
    assert partial.settlement["execution_truth_ref"] == partial.source_event_id
    assert partial.truth_source == "SIMULATOR"
    assert not partial.authoritative_external_truth

    second = runtime.on_bar(bar(2, "1000"))
    assert second[0].fill.quantity == Decimal("5.00")
    settlements = store.load_v1_reservation_settlements("paper-settle")
    assert [item.settlement["event"] for item in settlements] == ["PARTIAL_FILL", "FILLED"]
    assert settlements[-1].settlement["filled_quantity"] == "5.00"
    assert settlements[-1].settlement["remaining_quantity"] == "0.00"
    assert Decimal(settlements[0].settlement["filled_quantity"]) + Decimal(settlements[1].settlement["filled_quantity"]) == Decimal("10")
    assert store.verify_v1_execution_journal("paper-settle") == 3


def test_reservation_settlement_survives_restart_and_replays_with_fill_journal(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    legacy = order("10")
    first = PersistentPaperShadowRuntime(
        runtime_id="paper-settle-restart", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    first.start()
    bind_paper(first, legacy)
    first.on_bar(bar(1, "100"))
    first.stop()

    resumed = PersistentPaperShadowRuntime(
        runtime_id="paper-settle-restart", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    resumed.start()
    settlements = store.load_v1_reservation_settlements("paper-settle-restart")
    assert len(settlements) == 1
    assert settlements[0].settlement["event"] == "PARTIAL_FILL"
    assert store.verify_v1_execution_journal("paper-settle-restart") == 2


def test_corrupt_reservation_settlement_fails_closed_on_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-settle-corrupt", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    bind_paper(runtime, order("10"))
    runtime.on_bar(bar(1, "100"))
    runtime.stop()

    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                "UPDATE execution_v1_reservation_settlements SET settlement_json='{}' "
                "WHERE runtime_id='paper-settle-corrupt'"
            )

    resumed = PersistentPaperShadowRuntime(
        runtime_id="paper-settle-corrupt", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    with pytest.raises(CheckpointCorruptionError, match="V1 reservation settlement hash"):
        resumed.start()
    assert store.get_runtime("paper-settle-corrupt").status == "HALTED"


@pytest.mark.parametrize(
    ("event_type", "reject_code", "reject_reason", "expected_settlement"),
    [
        (ExecutionEventType.CANCELLED, None, None, "CANCELLED"),
        (ExecutionEventType.REJECTED, "FIXTURE_REJECT", "synthetic non-live rejection", "REJECTED"),
    ],
)
def test_shadow_terminal_cancel_and_reject_settle_without_adapter_side_effects(
    tmp_path, event_type, reject_code, reject_reason, expected_settlement
):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id=f"shadow-{expected_settlement.lower()}", mode=RuntimeMode.SHADOW, store=store,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order("3")
    intent = bind_shadow(runtime, legacy)
    event = ExecutionEvent.build(
        event_id=f"event-{expected_settlement.lower()}-001",
        execution_id=intent.execution_intent_id,
        client_order_id=str(legacy.order_id),
        venue_id="CRYPTO-FIXTURE",
        event_type=event_type,
        receive_timestamp=NOW + timedelta(seconds=1),
        reject_code=reject_code,
        reject_reason=reject_reason,
    )
    settlement = runtime.journal_v1_terminal_event(event)
    duplicate = runtime.journal_v1_terminal_event(event)

    assert duplicate == settlement
    assert settlement.event.value == expected_settlement
    assert settlement.filled_quantity == Decimal("0")
    assert settlement.remaining_quantity == Decimal("3")
    assert settlement.actual_notional == Decimal("0")
    assert settlement.actual_fees == Decimal("0")
    stored = store.load_v1_reservation_settlements(runtime.runtime_id)
    assert len(stored) == 1
    assert stored[0].source_event_id == event.event_id
    assert stored[0].truth_source == "SHADOW_PREVIEW"
    assert not stored[0].authoritative_external_truth
    assert adapter.submitted == []
    assert adapter.canceled == []


def test_atomic_event_settlement_conflict_rolls_back_new_execution_event(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-atomic", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order("10")
    intent = bind_paper(runtime, legacy)
    runtime.on_bar(bar(1, "100"))
    existing = store.load_v1_reservation_settlements("paper-atomic")[0]

    event = ExecutionEvent.build(
        event_id="event-cancel-conflict-001",
        execution_id=intent.execution_intent_id,
        client_order_id=str(legacy.order_id),
        venue_id="CRYPTO-FIXTURE",
        event_type=ExecutionEventType.CANCELLED,
        receive_timestamp=NOW + timedelta(minutes=1, seconds=1),
    )
    conflicting_payload = dict(existing.settlement)
    conflicting_payload.update({
        "event": "CANCELLED",
        "filled_quantity": "0",
        "remaining_quantity": "5.00",
        "actual_notional": "0",
        "actual_fees": "0",
        "timestamp": event.receive_timestamp.isoformat(),
        "execution_truth_ref": event.event_id,
    })
    with pytest.raises(ValueError, match="conflicting V1 reservation settlement"):
        store.append_v1_execution_event_with_settlement(
            "paper-atomic", runtime.owner_id,
            event_id=event.event_id, execution_intent_id=intent.execution_intent_id,
            client_order_id=str(legacy.order_id), runtime_mode="PAPER", truth_source="SIMULATOR",
            authoritative_external_truth=False, occurred_at=event.receive_timestamp, event=event.to_payload(),
            settlement_id=existing.settlement_id, reservation_id=existing.reservation_id,
            settlement=conflicting_payload,
        )
    assert event.event_id not in {item.event_id for item in store.load_v1_execution_events("paper-atomic")}
