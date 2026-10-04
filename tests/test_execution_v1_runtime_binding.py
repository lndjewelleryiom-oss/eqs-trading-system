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
    ExecutionEventType,
    ExecutionMode,
    InterlockIssuer,
    InterlockState,
    SubmissionInterlock,
    TimeInForce,
    V1AuthorityError,
    order_request_to_order_intent,
)
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import CheckpointCorruptionError, PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode, RuntimeStatus
from quant_system.shadow import ShadowExecutionEngine


NOW = datetime(2026, 9, 27, 19, 0, tzinfo=timezone.utc)
SEAL = "b" * 64


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


def test_paper_v1_binding_journals_authority_attempt_and_simulated_fills(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-v1", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order()
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    runtime.submit_v1_order(
        legacy, intent=intent, capability=capability(Capability.PUBLIC_ONLY), interlock=interlock(InterlockState.PAPER_ONLY),
        venue_adapter=canonical_adapter(Capability.PUBLIC_ONLY),
    )

    binding = store.get_v1_intent_binding("paper-v1", str(legacy.order_id))
    assert binding is not None
    assert binding.execution_intent_id == intent.execution_intent_id
    assert binding.intent["schema_version"] == "EQS-EXEC-ORDER-INTENT-v1.0"
    assert store.event_count("paper-v1", "V1_AUTHORITY_VERIFIED") == 1

    first = store.load_v1_execution_events("paper-v1")
    assert len(first) == 1
    assert first[0].event["event_type"] == ExecutionEventType.SUBMISSION_ATTEMPT.value
    assert first[0].truth_source == "SIMULATOR"
    assert not first[0].authoritative_external_truth

    runtime.on_bar(bar(1, "100"))
    runtime.on_bar(bar(2, "1000"))
    events = store.load_v1_execution_events("paper-v1")
    assert [item.event["event_type"] for item in events] == [
        ExecutionEventType.SUBMISSION_ATTEMPT.value,
        ExecutionEventType.PARTIAL_FILL.value,
        ExecutionEventType.FILL.value,
    ]
    assert all(item.truth_source == "SIMULATOR" for item in events)
    assert all(not item.authoritative_external_truth for item in events)
    assert store.verify_v1_execution_journal("paper-v1") == 3


def test_shadow_v1_binding_verifies_private_read_and_confirms_zero_submit(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shadow-v1", mode=RuntimeMode.SHADOW, store=store,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order("1")
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.SHADOW, Capability.PRIVATE_READ_ONLY))
    decision = runtime.submit_v1_order(
        legacy, intent=intent, capability=capability(Capability.PRIVATE_READ_ONLY),
        interlock=interlock(InterlockState.SHADOW_ZERO_SUBMIT),
        venue_adapter=canonical_adapter(Capability.PRIVATE_READ_ONLY, adapter),
    )
    assert not decision.submission_result.sent
    assert adapter.submitted == []
    events = store.load_v1_execution_events("shadow-v1")
    assert len(events) == 1
    assert events[0].event["event_type"] == ExecutionEventType.SUBMISSION_ATTEMPT.value
    assert events[0].truth_source == "SHADOW_PREVIEW"
    assert not events[0].authoritative_external_truth
    assert store.event_count("shadow-v1", "V1_AUTHORITY_VERIFIED") == 1
    assert store.event_count("shadow-v1", "V1_SHADOW_ZERO_SUBMIT_CONFIRMED") == 1


def test_v1_capability_mismatch_halts_before_legacy_engine_receives_order(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-block", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order()
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    wrong = ConnectionCapability(
        connection_id="wrong-connection", venue_id="CRYPTO-FIXTURE", capabilities=(Capability.PUBLIC_ONLY,),
        verified_at=NOW, verification_ref="capability-evidence-wrong", production_submission_enabled=False,
    )
    with pytest.raises(V1AuthorityError, match="connection capability does not match"):
        runtime.submit_v1_order(legacy, intent=intent, capability=wrong, interlock=interlock(InterlockState.PAPER_ONLY),
            venue_adapter=canonical_adapter(Capability.PUBLIC_ONLY))
    assert runtime.status == RuntimeStatus.HALTED
    assert runtime.paper_engine.simulator.pending_count == 0
    assert store.get_v1_intent_binding("paper-v1-block", str(legacy.order_id)) is None
    assert store.load_v1_execution_events("paper-v1-block") == ()
    assert store.event_count("paper-v1-block", "V1_AUTHORITY_REJECTED") == 1


def test_nonlive_runtime_rejects_broader_interlock_state(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-interlock", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order()
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    with pytest.raises(V1AuthorityError, match="requires exact interlock state PAPER_ONLY"):
        runtime.submit_v1_order(
            legacy, intent=intent, capability=capability(Capability.PUBLIC_ONLY),
            interlock=interlock(InterlockState.SHADOW_ZERO_SUBMIT),
            venue_adapter=canonical_adapter(Capability.PUBLIC_ONLY),
        )
    assert runtime.status == RuntimeStatus.HALTED


def test_v1_journal_survives_restart_and_is_verified(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    legacy = order()
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    first = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-restart", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    first.start()
    first.submit_v1_order(
        legacy, intent=intent, capability=capability(Capability.PUBLIC_ONLY), interlock=interlock(InterlockState.PAPER_ONLY),
        venue_adapter=canonical_adapter(Capability.PUBLIC_ONLY),
    )
    first.stop()

    resumed = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-restart", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    resumed.start()
    assert store.verify_v1_execution_journal("paper-v1-restart") == 1
    assert store.get_v1_intent_binding("paper-v1-restart", str(legacy.order_id)) is not None


def test_corrupt_v1_event_fails_closed_on_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    legacy = order()
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    first = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-corrupt", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    first.start()
    first.submit_v1_order(
        legacy, intent=intent, capability=capability(Capability.PUBLIC_ONLY), interlock=interlock(InterlockState.PAPER_ONLY),
        venue_adapter=canonical_adapter(Capability.PUBLIC_ONLY),
    )
    first.stop()
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("UPDATE execution_v1_events SET event_json='{}' WHERE runtime_id='paper-v1-corrupt'")

    resumed = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-corrupt", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    with pytest.raises(CheckpointCorruptionError, match="V1 execution event hash"):
        resumed.start()
    assert store.get_runtime("paper-v1-corrupt").status == "HALTED"


def test_legacy_submit_order_path_remains_unbound_and_unchanged(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-legacy", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order()
    runtime.submit_order(legacy)
    assert runtime.paper_engine.simulator.pending_count == 1
    assert store.load_v1_execution_events("paper-legacy") == ()
    assert store.get_v1_intent_binding("paper-legacy", str(legacy.order_id)) is None


def test_shadow_reconciliation_is_bound_into_v1_journal(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shadow-v1-recon", mode=RuntimeMode.SHADOW, store=store,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order("1")
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.SHADOW, Capability.PRIVATE_READ_ONLY))
    runtime.submit_v1_order(
        legacy, intent=intent, capability=capability(Capability.PRIVATE_READ_ONLY),
        interlock=interlock(InterlockState.SHADOW_ZERO_SUBMIT),
        venue_adapter=canonical_adapter(Capability.PRIVATE_READ_ONLY, adapter),
    )
    result = runtime.reconcile_shadow(expected_open_order_ids=set(), expected_positions={})
    assert result.action.value == "OK"
    assert store.event_count("shadow-v1-recon", "V1_RECONCILIATION_RESULT") == 1
    events = store.load_v1_execution_events("shadow-v1-recon")
    assert [item.event["event_type"] for item in events] == [
        ExecutionEventType.SUBMISSION_ATTEMPT.value,
        ExecutionEventType.RECONCILIATION_OBSERVATION.value,
    ]
    assert events[-1].truth_source == "SHADOW_PREVIEW"
    assert events[-1].event["raw_receipt_hash"] is not None
    assert not events[-1].authoritative_external_truth


def test_expired_interlock_is_rejected_before_execution(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-expired", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order()
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    expired = SubmissionInterlock(
        interlock_id="interlock-001", revision=1, state=InterlockState.PAPER_ONLY, issued_by=InterlockIssuer.EQS_00,
        valid_from=NOW - timedelta(minutes=10), valid_until=NOW - timedelta(seconds=1),
        programme_state_ref="programme-state-001", seal_hash=SEAL,
    )
    with pytest.raises(V1AuthorityError, match="outside its validity window"):
        runtime.submit_v1_order(
            legacy, intent=intent, capability=capability(Capability.PUBLIC_ONLY), interlock=expired,
            venue_adapter=canonical_adapter(Capability.PUBLIC_ONLY),
        )
    assert runtime.paper_engine.simulator.pending_count == 0
    assert runtime.status == RuntimeStatus.HALTED


def test_corrupt_v1_authority_binding_fails_closed_on_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    legacy = order()
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    first = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-binding-corrupt", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    first.start()
    first.submit_v1_order(
        legacy, intent=intent, capability=capability(Capability.PUBLIC_ONLY), interlock=interlock(InterlockState.PAPER_ONLY),
        venue_adapter=canonical_adapter(Capability.PUBLIC_ONLY),
    )
    first.stop()
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                "UPDATE execution_v1_bindings SET capability_json='{}' WHERE runtime_id='paper-v1-binding-corrupt'"
            )
    resumed = PersistentPaperShadowRuntime(
        runtime_id="paper-v1-binding-corrupt", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    with pytest.raises(CheckpointCorruptionError, match="V1 authority binding hash"):
        resumed.start()
    assert store.get_runtime("paper-v1-binding-corrupt").status == "HALTED"
