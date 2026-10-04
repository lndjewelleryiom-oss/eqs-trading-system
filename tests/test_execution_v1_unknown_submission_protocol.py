from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from uuid import uuid4

import pytest

from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest
from quant_system.execution.v1 import (
    BlindExternalActionRetryError,
    AssetClass,
    Capability,
    ConnectionCapability,
    ConstraintSnapshot,
    CryptoBrokerAdapterBridge,
    CryptoOrderIntentContext,
    ExecutionMode,
    ExternalActionKind,
    ExternalActionObservation,
    ExternalActionState,
    ExternalObservationStatus,
    InterlockIssuer,
    InterlockState,
    SubmissionInterlock,
    SyntheticUnknownOutcomeProtocol,
    TimeInForce,
    deterministic_external_action_id,
    order_request_to_order_intent,
)
from quant_system.runtime import (
    CheckpointCorruptionError,
    PersistentPaperShadowRuntime,
    PersistentRuntimeStore,
    RuntimeConfig,
    RuntimeMode,
)
from quant_system.shadow import ShadowExecutionEngine


NOW = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)
SEAL = "d" * 64


@dataclass
class SyntheticExternalTruth:
    submit_calls: dict[str, int] = field(default_factory=dict)
    cancel_calls: dict[str, int] = field(default_factory=dict)
    observations: dict[str, ExternalActionObservation] = field(default_factory=dict)

    def submit_side_effect(self, action_id: str) -> None:
        self.submit_calls[action_id] = self.submit_calls.get(action_id, 0) + 1
        self.observations[action_id] = ExternalActionObservation(
            ExternalObservationStatus.ACKNOWLEDGED,
            observation_ref=f"submit-observation-{action_id[-12:]}",
            external_order_id=f"SYN-{action_id[-12:]}",
        )

    def cancel_side_effect(self, action_id: str, external_order_id: str) -> None:
        self.cancel_calls[action_id] = self.cancel_calls.get(action_id, 0) + 1
        self.observations[action_id] = ExternalActionObservation(
            ExternalObservationStatus.CANCELLED,
            observation_ref=f"cancel-observation-{action_id[-12:]}",
            external_order_id=external_order_id,
        )

    def observe(self, action_id: str) -> ExternalActionObservation:
        return self.observations[action_id]


def order() -> OrderRequest:
    return OrderRequest(
        strategy_id=uuid4(),
        symbol="BTC-PERP",
        side=Side.BUY,
        quantity=Decimal("1"),
        order_type=OrderType.MARKET,
        decision_time=NOW,
        reference_price=Decimal("100"),
    )


def context() -> CryptoOrderIntentContext:
    return CryptoOrderIntentContext(
        portfolio_decision_id="portfolio-decision-001",
        portfolio_id="crypto-portfolio",
        reservation_id="reservation-001",
        risk_authorisation_id="risk-auth-001",
        authorised_at=NOW - timedelta(seconds=10),
        expires_at=NOW + timedelta(minutes=30),
        venue_id="CRYPTO-FIXTURE",
        connection_id="crypto-fixture-main",
        required_capability=Capability.PRIVATE_READ_ONLY,
        mode=ExecutionMode.SHADOW,
        programme_state_ref="programme-state-001",
        interlock_ref="interlock-001",
        market_state_ref="market-state-001",
        reference_data_ref="reference-data-001",
        instrument_id="CRYPTO:PERP:BTC",
        currency="USD",
        strategy_version="legacy-crypto-v1",
        time_in_force=TimeInForce.GTC,
        asset_extension={"product_type": "PERPETUAL"},
    )


def capability() -> ConnectionCapability:
    return ConnectionCapability(
        connection_id="crypto-fixture-main",
        venue_id="CRYPTO-FIXTURE",
        capabilities=(Capability.PRIVATE_READ_ONLY,),
        verified_at=NOW - timedelta(seconds=5),
        verification_ref="capability-evidence-001",
        production_submission_enabled=False,
    )


def interlock() -> SubmissionInterlock:
    return SubmissionInterlock(
        interlock_id="interlock-001",
        revision=1,
        state=InterlockState.SHADOW_ZERO_SUBMIT,
        issued_by=InterlockIssuer.EQS_00,
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(hours=1),
        programme_state_ref="programme-state-001",
        seal_hash=SEAL,
    )


def canonical_adapter(required: Capability, adapter=None, *, state_status: str = "TRADING") -> CryptoBrokerAdapterBridge:
    adapter = adapter or InMemoryBrokerAdapter()
    if required != Capability.PRIVATE_READ_ONLY:
        raise ValueError("unknown-outcome fixture only supports PRIVATE_READ_ONLY")
    cap = capability()
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


def shadow_runtime(path, runtime_id: str, at: datetime, *, lease_seconds: int = 30):
    store = PersistentRuntimeStore(path)
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id=runtime_id,
        mode=RuntimeMode.SHADOW,
        store=store,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
        config=RuntimeConfig(lease_seconds=lease_seconds),
        clock=lambda: at,
    )
    return store, runtime, adapter


def bind(runtime: PersistentPaperShadowRuntime, legacy: OrderRequest):
    intent = order_request_to_order_intent(legacy, context())
    runtime.submit_v1_order(
        legacy, intent=intent, capability=capability(), interlock=interlock(),
        venue_adapter=canonical_adapter(Capability.PRIVATE_READ_ONLY, runtime.shadow_engine.gateway.adapter),
    )
    return intent


def test_deterministic_external_action_id_is_stable_and_attempt_scoped():
    values = dict(
        execution_intent_id="execution-intent-001",
        client_order_id="client-order-001",
        action_kind=ExternalActionKind.SUBMIT,
        attempt=1,
    )
    first = deterministic_external_action_id(**values)
    second = deterministic_external_action_id(**values)
    retry = deterministic_external_action_id(**{**values, "attempt": 2, "parent_action_id": first})
    assert first == second
    assert retry != first
    assert first.startswith("ACT-") and retry.startswith("ACT-")


def test_ambiguous_submit_crash_boundary_recovers_reconciles_and_never_blind_resubmits(tmp_path):
    path = tmp_path / "runtime.db"
    store, first, shadow_adapter = shadow_runtime(path, "submit-crash", NOW, lease_seconds=1)
    first.start()
    legacy = order()
    intent = bind(first, legacy)
    protocol = SyntheticUnknownOutcomeProtocol(store=store, runtime_id="submit-crash", owner_id=first.owner_id)
    prepared = protocol.prepare_submit(intent, client_order_id=str(legacy.order_id), at=NOW)
    protocol.begin_dispatch(prepared.action_id, at=NOW + timedelta(milliseconds=1))

    external = SyntheticExternalTruth()
    external.submit_side_effect(prepared.action_id)  # side effect happened; response is lost with the process
    assert shadow_adapter.submitted == []  # synthetic harness is outside BrokerGateway

    restarted_at = NOW + timedelta(seconds=2)
    store2, resumed, resumed_adapter = shadow_runtime(path, "submit-crash", restarted_at, lease_seconds=30)
    resumed.start()  # prior owner lease expired: unclean process replacement
    recovered_protocol = SyntheticUnknownOutcomeProtocol(
        store=store2, runtime_id="submit-crash", owner_id=resumed.owner_id
    )
    assert recovered_protocol.recover_after_restart(at=restarted_at) == (prepared.action_id,)
    latest = store2.get_v1_external_action_latest("submit-crash", prepared.action_id)
    assert latest.state == ExternalActionState.RECONCILIATION_REQUIRED.value
    states = [x.state for x in store2.load_v1_external_action_transitions("submit-crash") if x.action_id == prepared.action_id]
    assert states == [
        ExternalActionState.PREPARED.value,
        ExternalActionState.DISPATCHING.value,
        ExternalActionState.UNKNOWN_SUBMISSION_OUTCOME.value,
        ExternalActionState.RECONCILIATION_REQUIRED.value,
    ]
    assert states.count(ExternalActionState.UNKNOWN_SUBMISSION_OUTCOME.value) == 1

    with pytest.raises(BlindExternalActionRetryError, match="blind external-action retry blocked"):
        recovered_protocol.begin_dispatch(prepared.action_id, at=restarted_at + timedelta(milliseconds=1))
    assert external.submit_calls[prepared.action_id] == 1

    resolved = recovered_protocol.reconcile(
        prepared.action_id,
        external.observe(prepared.action_id),
        at=restarted_at + timedelta(milliseconds=2),
    )
    assert resolved.state == ExternalActionState.RESOLVED_ACKNOWLEDGED.value
    assert resolved.payload["reconciliation"]["status"] == "MATCHED"
    assert resolved.payload["execution_event"]["event_type"] == "ACKNOWLEDGED"
    assert resumed_adapter.submitted == []
    assert store2.verify_v1_external_action_journal("submit-crash") == 5


def test_unknown_submit_requires_reconciliation_before_explicit_retry(tmp_path):
    path = tmp_path / "runtime.db"
    store, runtime, adapter = shadow_runtime(path, "submit-absent", NOW)
    runtime.start()
    legacy = order()
    intent = bind(runtime, legacy)
    protocol = SyntheticUnknownOutcomeProtocol(store=store, runtime_id="submit-absent", owner_id=runtime.owner_id)
    first = protocol.prepare_submit(intent, client_order_id=str(legacy.order_id), at=NOW)
    protocol.begin_dispatch(first.action_id, at=NOW + timedelta(milliseconds=1))
    protocol.mark_ambiguous(first.action_id, at=NOW + timedelta(milliseconds=2), reason="TIMEOUT_AFTER_WRITE")

    with pytest.raises(BlindExternalActionRetryError):
        protocol.begin_dispatch(first.action_id, at=NOW + timedelta(milliseconds=3))

    absent = protocol.reconcile(
        first.action_id,
        ExternalActionObservation(ExternalObservationStatus.ABSENT, "recon-proof-absent-001"),
        at=NOW + timedelta(milliseconds=4),
    )
    assert absent.state == ExternalActionState.RESOLVED_ABSENT.value
    retry = protocol.prepare_retry_after_absence(first.action_id, intent, at=NOW + timedelta(milliseconds=5))
    assert retry.attempt == 2
    assert retry.parent_action_id == first.action_id
    assert retry.action_id != first.action_id
    protocol.begin_dispatch(retry.action_id, at=NOW + timedelta(milliseconds=6))
    assert adapter.submitted == []


def test_ambiguous_cancel_crash_boundary_resolves_only_from_terminal_truth(tmp_path):
    path = tmp_path / "runtime.db"
    store, first, shadow_adapter = shadow_runtime(path, "cancel-crash", NOW, lease_seconds=1)
    first.start()
    legacy = order()
    intent = bind(first, legacy)
    protocol = SyntheticUnknownOutcomeProtocol(store=store, runtime_id="cancel-crash", owner_id=first.owner_id)
    prepared = protocol.prepare_cancel(
        intent,
        client_order_id=str(legacy.order_id),
        external_order_id="SYN-EXTERNAL-001",
        at=NOW,
    )
    protocol.begin_dispatch(prepared.action_id, at=NOW + timedelta(milliseconds=1))
    external = SyntheticExternalTruth()
    external.cancel_side_effect(prepared.action_id, "SYN-EXTERNAL-001")
    assert shadow_adapter.canceled == []

    restarted_at = NOW + timedelta(seconds=2)
    store2, resumed, resumed_adapter = shadow_runtime(path, "cancel-crash", restarted_at)
    resumed.start()
    recovered = SyntheticUnknownOutcomeProtocol(store=store2, runtime_id="cancel-crash", owner_id=resumed.owner_id)
    recovered.recover_after_restart(at=restarted_at)
    latest = store2.get_v1_external_action_latest("cancel-crash", prepared.action_id)
    assert latest.state == ExternalActionState.RECONCILIATION_REQUIRED.value
    with pytest.raises(BlindExternalActionRetryError):
        recovered.begin_dispatch(prepared.action_id, at=restarted_at + timedelta(milliseconds=1))

    resolved = recovered.reconcile(
        prepared.action_id,
        external.observe(prepared.action_id),
        at=restarted_at + timedelta(milliseconds=2),
    )
    assert resolved.state == ExternalActionState.RESOLVED_CANCELLED.value
    assert resolved.payload["execution_event"]["event_type"] == "CANCELLED"
    assert external.cancel_calls[prepared.action_id] == 1
    assert resumed_adapter.canceled == []


def test_cancel_absence_does_not_fabricate_terminal_resolution(tmp_path):
    store, runtime, _ = shadow_runtime(tmp_path / "runtime.db", "cancel-absent", NOW)
    runtime.start()
    legacy = order()
    intent = bind(runtime, legacy)
    protocol = SyntheticUnknownOutcomeProtocol(store=store, runtime_id="cancel-absent", owner_id=runtime.owner_id)
    action = protocol.prepare_cancel(
        intent, client_order_id=str(legacy.order_id), external_order_id="SYN-EXT-002", at=NOW
    )
    protocol.begin_dispatch(action.action_id, at=NOW + timedelta(milliseconds=1))
    protocol.mark_ambiguous(action.action_id, at=NOW + timedelta(milliseconds=2), reason="CANCEL_TIMEOUT")
    still_unknown = protocol.reconcile(
        action.action_id,
        ExternalActionObservation(ExternalObservationStatus.ABSENT, "cancel-absence-evidence-001"),
        at=NOW + timedelta(milliseconds=3),
    )
    assert still_unknown.state == ExternalActionState.RECONCILIATION_REQUIRED.value
    assert still_unknown.payload["reconciliation"]["status"] == "BLOCKED_UNKNOWN"
    assert still_unknown.payload["reconciliation"]["unresolved"][0]["reason"] == "CANCEL_TARGET_ABSENT_WITHOUT_TERMINAL_TRUTH"


def test_repeated_unknown_reconciliation_stays_blocked_and_is_durable(tmp_path):
    store, runtime, _ = shadow_runtime(tmp_path / "runtime.db", "still-unknown", NOW)
    runtime.start()
    legacy = order()
    intent = bind(runtime, legacy)
    protocol = SyntheticUnknownOutcomeProtocol(store=store, runtime_id="still-unknown", owner_id=runtime.owner_id)
    action = protocol.prepare_submit(intent, client_order_id=str(legacy.order_id), at=NOW)
    protocol.begin_dispatch(action.action_id, at=NOW + timedelta(milliseconds=1))
    protocol.mark_ambiguous(action.action_id, at=NOW + timedelta(milliseconds=2), reason="NETWORK_LOSS")
    checked = protocol.reconcile(
        action.action_id,
        ExternalActionObservation(ExternalObservationStatus.UNKNOWN, "recon-still-unknown-001"),
        at=NOW + timedelta(milliseconds=3),
    )
    assert checked.state == ExternalActionState.RECONCILIATION_REQUIRED.value
    assert checked.payload["reconciliation"]["status"] == "BLOCKED_UNKNOWN"
    with pytest.raises(BlindExternalActionRetryError):
        protocol.begin_dispatch(action.action_id, at=NOW + timedelta(milliseconds=4))
    assert store.verify_v1_external_action_journal("still-unknown") == 5


def test_external_action_journal_corruption_fails_closed_on_runtime_restart(tmp_path):
    path = tmp_path / "runtime.db"
    store, runtime, _ = shadow_runtime(path, "action-corrupt", NOW)
    runtime.start()
    legacy = order()
    intent = bind(runtime, legacy)
    protocol = SyntheticUnknownOutcomeProtocol(store=store, runtime_id="action-corrupt", owner_id=runtime.owner_id)
    action = protocol.prepare_submit(intent, client_order_id=str(legacy.order_id), at=NOW)
    protocol.begin_dispatch(action.action_id, at=NOW + timedelta(milliseconds=1))
    runtime.stop()

    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                "UPDATE execution_v1_external_action_journal SET payload_json='{}' "
                "WHERE runtime_id='action-corrupt' AND state='DISPATCHING'"
            )

    store2, resumed, _ = shadow_runtime(path, "action-corrupt", NOW + timedelta(seconds=1))
    with pytest.raises(CheckpointCorruptionError, match="external action transition hash"):
        resumed.start()
    assert store2.get_runtime("action-corrupt").status == "HALTED"


def test_synthetic_protocol_never_mutates_nonlive_broker_gateway(tmp_path):
    store, runtime, adapter = shadow_runtime(tmp_path / "runtime.db", "no-broker-mutation", NOW)
    runtime.start()
    legacy = order()
    intent = bind(runtime, legacy)
    protocol = SyntheticUnknownOutcomeProtocol(store=store, runtime_id="no-broker-mutation", owner_id=runtime.owner_id)
    action = protocol.prepare_submit(intent, client_order_id=str(legacy.order_id), at=NOW)
    protocol.begin_dispatch(action.action_id, at=NOW + timedelta(milliseconds=1))
    protocol.mark_ambiguous(action.action_id, at=NOW + timedelta(milliseconds=2), reason="SYNTHETIC_ONLY")
    assert adapter.submitted == []
    assert adapter.canceled == []
    assert runtime.shadow_engine.gateway.venue_submission_enabled is False
