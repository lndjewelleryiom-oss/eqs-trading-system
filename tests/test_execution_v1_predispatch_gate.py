from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from uuid import uuid4

import pytest

from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest
from quant_system.execution.v1 import (
    AdapterError,
    AdapterErrorCategory,
    AdapterResult,
    AssetClass,
    Capability,
    ConnectionCapability,
    ConstraintSnapshot,
    CryptoOrderIntentContext,
    ExecutionMode,
    InterlockIssuer,
    InterlockState,
    MarketSessionState,
    SessionState,
    SubmissionInterlock,
    TimeInForce,
    V1PreDispatchError,
    VenueOperationalState,
    VenueState,
    order_request_to_order_intent,
)
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import (
    CheckpointCorruptionError,
    PersistentPaperShadowRuntime,
    PersistentRuntimeStore,
    RuntimeMode,
    RuntimeStatus,
)
from quant_system.shadow import ShadowExecutionEngine


NOW = datetime(2026, 9, 27, 21, 30, tzinfo=timezone.utc)
SEAL = "e" * 64


def assumptions() -> ExecutionAssumptions:
    return ExecutionAssumptions(
        commission_bps=Decimal("1"), spread_bps=Decimal("2"), slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"), financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("0"), latency_ms=0,
    )


def paper_engine() -> PaperTradingEngine:
    return PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions()), initial_cash=Decimal("10000"))


def order() -> OrderRequest:
    return OrderRequest(
        strategy_id=uuid4(), symbol="BTC-PERP", side=Side.BUY, quantity=Decimal("1"),
        order_type=OrderType.MARKET, decision_time=NOW, reference_price=Decimal("100"),
    )


def context(mode: ExecutionMode, required: Capability) -> CryptoOrderIntentContext:
    return CryptoOrderIntentContext(
        portfolio_decision_id="portfolio-decision-001", portfolio_id="crypto-portfolio",
        reservation_id="reservation-001", risk_authorisation_id="risk-auth-001",
        authorised_at=NOW - timedelta(seconds=10), expires_at=NOW + timedelta(minutes=30),
        venue_id="CRYPTO-FIXTURE", connection_id="crypto-fixture-main", required_capability=required,
        mode=mode, programme_state_ref="programme-state-001", interlock_ref="interlock-001",
        market_state_ref="market-state-001", reference_data_ref="reference-data-001",
        instrument_id="CRYPTO:PERP:BTC", currency="USD", strategy_version="legacy-crypto-v1",
        time_in_force=TimeInForce.GTC, asset_extension={"product_type": "PERPETUAL"},
    )


def capability(required: Capability, *, verification_ref: str = "capability-evidence-001") -> ConnectionCapability:
    return ConnectionCapability(
        connection_id="crypto-fixture-main", venue_id="CRYPTO-FIXTURE", capabilities=(required,),
        verified_at=NOW - timedelta(seconds=5), verification_ref=verification_ref,
        production_submission_enabled=False,
    )


def interlock(mode: ExecutionMode) -> SubmissionInterlock:
    state = InterlockState.PAPER_ONLY if mode == ExecutionMode.PAPER else InterlockState.SHADOW_ZERO_SUBMIT
    return SubmissionInterlock(
        interlock_id="interlock-001", revision=1, state=state, issued_by=InterlockIssuer.EQS_00,
        valid_from=NOW - timedelta(minutes=1), valid_until=NOW + timedelta(hours=1),
        programme_state_ref="programme-state-001", seal_hash=SEAL,
    )


def constraints(*, observed_at: datetime | None = None, valid_until: datetime | None = None, status: str = "TRADING") -> ConstraintSnapshot:
    return ConstraintSnapshot(
        snapshot_id="constraint-fixture-001", asset_class=AssetClass.CRYPTO,
        instrument_id="CRYPTO:PERP:BTC", venue_id="CRYPTO-FIXTURE", venue_symbol="BTC-PERP",
        observed_at=observed_at or NOW - timedelta(seconds=1),
        valid_until=valid_until or NOW + timedelta(minutes=10),
        source_ref="fixture-reference-data-001", source_version="fixture-v1", status=status,
        tick_size=Decimal("1"), lot_size=Decimal("1"), asset_extension={"product_type": "PERPETUAL"},
    )


@dataclass
class GateAdapter:
    connection_capability: ConnectionCapability
    constraint: ConstraintSnapshot
    venue: VenueState
    session: MarketSessionState
    venue_error: AdapterError | None = None
    session_error: AdapterError | None = None
    constraint_error: AdapterError | None = None

    def get_instrument_constraints(self, instrument_id: str, *, at: datetime):
        if self.constraint_error is not None:
            return AdapterResult.failure(self.constraint_error, at)
        return AdapterResult.success(self.constraint, at)

    def get_venue_state(self, *, at: datetime):
        if self.venue_error is not None:
            return AdapterResult.failure(self.venue_error, at)
        return AdapterResult.success(self.venue, at)

    def get_market_session_state(self, instrument_id: str, *, at: datetime):
        if self.session_error is not None:
            return AdapterResult.failure(self.session_error, at)
        return AdapterResult.success(self.session, at)

    # The pre-dispatch gate must not touch private-read or trading operations.
    def get_account_state(self, *, at: datetime):
        raise AssertionError("pre-dispatch gate must not read account state")

    def list_open_orders(self, *, at: datetime):
        raise AssertionError("pre-dispatch gate must not read open orders")

    def list_recent_fills(self, *, at: datetime):
        raise AssertionError("pre-dispatch gate must not read fills")

    def get_positions(self, *, at: datetime):
        raise AssertionError("pre-dispatch gate must not read positions")

    def get_order(self, client_order_id: str, *, at: datetime):
        raise AssertionError("pre-dispatch gate must not read an order")

    def submit_order(self, intent, *, action_id: str, at: datetime):
        raise AssertionError("pre-dispatch gate must never submit")

    def cancel_order(self, client_order_id: str, *, action_id: str, at: datetime):
        raise AssertionError("pre-dispatch gate must never cancel")


def gate_adapter(
    required: Capability,
    *,
    constraint: ConstraintSnapshot | None = None,
    venue_state: VenueOperationalState = VenueOperationalState.TRADING,
    venue_observed_at: datetime | None = None,
    session_state: SessionState = SessionState.OPEN,
    session_observed_at: datetime | None = None,
    adapter_capability: ConnectionCapability | None = None,
    venue_error: AdapterError | None = None,
) -> GateAdapter:
    return GateAdapter(
        connection_capability=adapter_capability or capability(required),
        constraint=constraint or constraints(),
        venue=VenueState(
            venue_id="CRYPTO-FIXTURE", state=venue_state,
            observed_at=venue_observed_at or NOW, reason="FIXTURE",
        ),
        session=MarketSessionState(
            venue_id="CRYPTO-FIXTURE", instrument_id="CRYPTO:PERP:BTC", state=session_state,
            observed_at=session_observed_at or NOW, session_label="CRYPTO_24X7",
        ),
        venue_error=venue_error,
    )


def paper_runtime(path, runtime_id: str = "paper-predispatch"):
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(
        runtime_id=runtime_id, mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    return store, runtime


def bind(runtime: PersistentPaperShadowRuntime, legacy: OrderRequest, adapter: GateAdapter):
    cap = capability(Capability.PUBLIC_ONLY)
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.PAPER, Capability.PUBLIC_ONLY))
    return runtime.submit_v1_order(
        legacy, intent=intent, capability=cap, interlock=interlock(ExecutionMode.PAPER), venue_adapter=adapter,
    ), intent


def test_runtime_predispatch_persists_exact_snapshot_hashes_before_paper_engine(tmp_path):
    store, runtime = paper_runtime(tmp_path / "runtime.db")
    runtime.start()
    legacy = order()
    adapter = gate_adapter(Capability.PUBLIC_ONLY)
    _, intent = bind(runtime, legacy, adapter)

    record = store.get_v1_pre_dispatch("paper-predispatch", str(legacy.order_id))
    assert record is not None
    assert record.execution_intent_id == intent.execution_intent_id
    assert record.verification["constraint_snapshot_hash"] == adapter.constraint.payload_hash
    assert record.verification["venue_state_hash"] == adapter.venue.payload_hash
    assert record.verification["session_state_hash"] == adapter.session.payload_hash
    assert record.verification["constraint_snapshot"] == adapter.constraint.to_payload()
    assert record.verification["venue_state_snapshot"] == adapter.venue.to_payload()
    assert record.verification["session_state_snapshot"] == adapter.session.to_payload()
    assert store.verify_v1_pre_dispatch_journal("paper-predispatch") == 1
    assert runtime.paper_engine.simulator.pending_count == 1
    event_types = [item.event_type for item in store.load_events("paper-predispatch")]
    assert event_types.index("V1_PRE_DISPATCH_VERIFIED") < event_types.index("ORDER_EVALUATED")


def test_stale_constraint_snapshot_halts_before_engine_dispatch(tmp_path):
    store, runtime = paper_runtime(tmp_path / "runtime.db", "stale-constraint")
    runtime.start()
    legacy = order()
    stale = constraints(observed_at=NOW - timedelta(minutes=5), valid_until=NOW - timedelta(seconds=1))
    with pytest.raises(V1PreDispatchError, match="constraint validation failed"):
        bind(runtime, legacy, gate_adapter(Capability.PUBLIC_ONLY, constraint=stale))
    assert runtime.status == RuntimeStatus.HALTED
    assert runtime.paper_engine.simulator.pending_count == 0
    assert store.get_v1_pre_dispatch("stale-constraint", str(legacy.order_id)) is None
    assert store.event_count("stale-constraint", "V1_PRE_DISPATCH_REJECTED") == 1


def test_degraded_venue_halts_before_engine_dispatch(tmp_path):
    _, runtime = paper_runtime(tmp_path / "runtime.db", "degraded-venue")
    runtime.start()
    with pytest.raises(V1PreDispatchError, match="venue state is not dispatchable: DEGRADED"):
        bind(runtime, order(), gate_adapter(Capability.PUBLIC_ONLY, venue_state=VenueOperationalState.DEGRADED))
    assert runtime.paper_engine.simulator.pending_count == 0


def test_closed_market_session_halts_before_engine_dispatch(tmp_path):
    _, runtime = paper_runtime(tmp_path / "runtime.db", "closed-session")
    runtime.start()
    with pytest.raises(V1PreDispatchError, match="market session is not dispatchable: CLOSED"):
        bind(runtime, order(), gate_adapter(Capability.PUBLIC_ONLY, session_state=SessionState.CLOSED))
    assert runtime.paper_engine.simulator.pending_count == 0


def test_stale_venue_or_session_observation_halts_before_dispatch(tmp_path):
    _, runtime = paper_runtime(tmp_path / "venue.db", "stale-venue")
    runtime.start()
    with pytest.raises(V1PreDispatchError, match="venue state is stale"):
        bind(runtime, order(), gate_adapter(Capability.PUBLIC_ONLY, venue_observed_at=NOW - timedelta(seconds=31)))

    _, runtime2 = paper_runtime(tmp_path / "session.db", "stale-session")
    runtime2.start()
    with pytest.raises(V1PreDispatchError, match="market session state is stale"):
        bind(runtime2, order(), gate_adapter(Capability.PUBLIC_ONLY, session_observed_at=NOW - timedelta(seconds=31)))


def test_adapter_capability_mismatch_is_rejected_before_dispatch(tmp_path):
    _, runtime = paper_runtime(tmp_path / "runtime.db", "adapter-capability-mismatch")
    runtime.start()
    mismatched = capability(Capability.PUBLIC_ONLY, verification_ref="different-capability-evidence")
    with pytest.raises(V1PreDispatchError, match="adapter capability evidence does not match"):
        bind(runtime, order(), gate_adapter(Capability.PUBLIC_ONLY, adapter_capability=mismatched))
    assert runtime.paper_engine.simulator.pending_count == 0


def test_unknown_or_unavailable_venue_state_fails_closed(tmp_path):
    _, runtime = paper_runtime(tmp_path / "runtime.db", "unknown-venue")
    runtime.start()
    error = AdapterError(AdapterErrorCategory.STALE_STATE, "VENUE_STATE_STALE", "venue state unavailable")
    with pytest.raises(V1PreDispatchError, match="venue state unavailable: STALE_STATE:VENUE_STATE_STALE"):
        bind(runtime, order(), gate_adapter(Capability.PUBLIC_ONLY, venue_error=error))
    assert runtime.paper_engine.simulator.pending_count == 0


def test_corrupt_or_missing_predispatch_evidence_fails_closed_on_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store, runtime = paper_runtime(path, "predispatch-corrupt")
    runtime.start()
    legacy = order()
    bind(runtime, legacy, gate_adapter(Capability.PUBLIC_ONLY))
    runtime.stop()
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                "UPDATE execution_v1_pre_dispatch SET verification_json='{}' WHERE runtime_id='predispatch-corrupt'"
            )
    resumed = PersistentPaperShadowRuntime(
        runtime_id="predispatch-corrupt", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    with pytest.raises(CheckpointCorruptionError, match="pre-dispatch verification hash"):
        resumed.start()
    assert store.get_runtime("predispatch-corrupt").status == "HALTED"

    path2 = tmp_path / "missing.db"
    store2, runtime2 = paper_runtime(path2, "predispatch-missing")
    runtime2.start()
    bind(runtime2, order(), gate_adapter(Capability.PUBLIC_ONLY))
    runtime2.stop()
    with closing(sqlite3.connect(path2)) as connection:
        with connection:
            connection.execute("DELETE FROM execution_v1_pre_dispatch WHERE runtime_id='predispatch-missing'")
    resumed2 = PersistentPaperShadowRuntime(
        runtime_id="predispatch-missing", mode=RuntimeMode.PAPER, store=store2, paper_engine=paper_engine(), clock=lambda: NOW,
    )
    with pytest.raises(CheckpointCorruptionError, match="missing required pre-dispatch verification"):
        resumed2.start()


def test_shadow_predispatch_gate_preserves_hard_zero_submit(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    broker = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shadow-predispatch", mode=RuntimeMode.SHADOW, store=store,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(broker, venue_submission_enabled=False)), clock=lambda: NOW,
    )
    runtime.start()
    legacy = order()
    cap = capability(Capability.PRIVATE_READ_ONLY)
    intent = order_request_to_order_intent(legacy, context(ExecutionMode.SHADOW, Capability.PRIVATE_READ_ONLY))
    decision = runtime.submit_v1_order(
        legacy, intent=intent, capability=cap, interlock=interlock(ExecutionMode.SHADOW),
        venue_adapter=gate_adapter(Capability.PRIVATE_READ_ONLY),
    )
    assert not decision.submission_result.sent
    assert broker.submitted == []
    assert broker.canceled == []
    assert store.event_count("shadow-predispatch", "V1_PRE_DISPATCH_VERIFIED") == 1
