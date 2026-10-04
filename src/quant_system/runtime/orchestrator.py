from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any, Callable, TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from quant_system.monitoring.shared06 import OperationalCertificationBoundary

from quant_system.monitoring.shared06 import OperationalCertificationBlocked, OperationalCertificationDecision
from quant_system.execution.capital_guard import CapitalGuard, SubmissionLifecycle
from quant_system.execution.private_reconciliation import PrivateStateReconciler
from quant_system.risk.execution_bridge import CapitalExecutionAuthorityV1

from quant_system.backtest.events import BarEvent
from quant_system.execution.models import OrderRequest
from quant_system.execution.reconciliation import ExecutionReconciler, ReconciliationAction
from quant_system.execution.v1 import (
    AssetClass,
    Capability,
    CanonicalVenueAdapter,
    ConnectionCapability,
    ExecutionEvent,
    ExecutionEventType,
    ExecutionMode,
    JournalTruthSource,
    OrderIntent,
    ReservationSettlement,
    SubmissionInterlock,
    V1AuthorityError,
    PrivateReadExpectation,
    PrivateReadReconciliationReport,
    ReconciliationCadencePolicy,
    ReconciliationWatchdogStatus,
    TradingCommissioningBlocked,
    assess_private_read_drift,
    advance_recovery_context,
    assess_reconciliation_watchdog,
    make_reconciliation_policy,
    reconciliation_due,
    execution_event_to_reservation_settlement,
    execution_reconciliation_to_v1,
    order_intent_to_submission_attempt_event,
    paper_fill_to_execution_event,
    reconciliation_observation_to_execution_event,
    reconcile_private_read,
    verify_nonlive_execution_authority,
    verify_v1_pre_dispatch,
)
from quant_system.monitoring import DegradationAssessment, DegradationMonitor, DegradationState, ExpectedBehavior, ObservedBehavior
from quant_system.paper import PaperTradingEngine
from quant_system.shadow import ShadowExecutionEngine

from .codec import decode_paper_state, decode_shadow_state, encode_order, encode_paper_state, encode_shadow_state
from .store import PersistentRuntimeStore, RuntimeLeaseError, payload_hash


class DuplicateOrderError(RuntimeError):
    pass


class FrozenWorkstreamExecutionRejected(RuntimeError):
    code = "EQS00_EXECUTION_OPTIONS_DISABLED"

    def __init__(self) -> None:
        super().__init__(self.code)


class RuntimeMode(StrEnum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"


class RuntimeStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"
    STOPPED = "STOPPED"


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    lease_seconds: int = 30
    max_consecutive_failures: int = 3
    v1_state_max_age_seconds: int = 30
    v1_private_reconciliation_max_age_seconds: int = 30
    v1_private_reconciliation_cadence_seconds: int = 15
    v1_private_reconciliation_watchdog_max_age_seconds: int = 30

    def __post_init__(self) -> None:
        if self.lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if self.max_consecutive_failures <= 0:
            raise ValueError("max_consecutive_failures must be positive")
        if self.v1_state_max_age_seconds <= 0:
            raise ValueError("v1_state_max_age_seconds must be positive")
        if self.v1_private_reconciliation_max_age_seconds <= 0:
            raise ValueError("v1_private_reconciliation_max_age_seconds must be positive")
        if self.v1_private_reconciliation_cadence_seconds <= 0:
            raise ValueError("v1_private_reconciliation_cadence_seconds must be positive")
        if self.v1_private_reconciliation_watchdog_max_age_seconds < self.v1_private_reconciliation_cadence_seconds:
            raise ValueError("v1_private_reconciliation_watchdog_max_age_seconds must be >= cadence")


@dataclass(slots=True)
class RuntimeMetrics:
    orders_received: int = 0
    paper_fills: int = 0
    shadow_decisions: int = 0
    reconciliations: int = 0
    reconciliation_failures: int = 0
    degradation_checks: int = 0
    degradation_pauses: int = 0
    errors: int = 0
    restarts: int = 0
    recovery_attempts: int = 0
    recovery_successes: int = 0
    heartbeats: int = 0


@dataclass(frozen=True, slots=True)
class RuntimeHealth:
    runtime_id: str
    mode: RuntimeMode
    status: RuntimeStatus
    halt_reason: str | None
    generation: int
    owner_id: str
    metrics: dict[str, int]
    last_market_event_at: datetime | None
    last_reconcile_at: datetime | None
    last_degradation_at: datetime | None
    consecutive_failures: int


class PersistentPaperShadowRuntime:
    """Restart-safe paper/shadow runtime with a hard no-submit shadow invariant.

    This class intentionally exposes no method for enabling broker submission or loading
    credentials. A SHADOW runtime verifies the disabled flag at construction and again
    before every broker-facing decision or reconciliation read.
    """

    STATE_SCHEMA_VERSION = 1

    def __init__(
        self,
        *,
        runtime_id: str,
        mode: RuntimeMode,
        store: PersistentRuntimeStore,
        paper_engine: PaperTradingEngine | None = None,
        shadow_engine: ShadowExecutionEngine | None = None,
        config: RuntimeConfig = RuntimeConfig(),
        reconciler: ExecutionReconciler | None = None,
        degradation_monitor: DegradationMonitor | None = None,
        clock: Callable[[], datetime] | None = None,
        operational_boundary: "OperationalCertificationBoundary | None" = None,
    ) -> None:
        if not runtime_id.strip():
            raise ValueError("runtime_id is required")
        if mode == RuntimeMode.PAPER and (paper_engine is None or shadow_engine is not None):
            raise ValueError("PAPER runtime requires only paper_engine")
        if mode == RuntimeMode.SHADOW and (shadow_engine is None or paper_engine is not None):
            raise ValueError("SHADOW runtime requires only shadow_engine")
        if shadow_engine is not None and shadow_engine.gateway.venue_submission_enabled:
            raise ValueError("SHADOW runtime requires venue submission physically disabled")
        self.runtime_id = runtime_id
        self.mode = mode
        self.store = store
        self.paper_engine = paper_engine
        self.shadow_engine = shadow_engine
        self.config = config
        self.reconciler = reconciler or ExecutionReconciler()
        self.degradation_monitor = degradation_monitor or DegradationMonitor()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.operational_boundary = operational_boundary
        self.owner_id = str(uuid4())
        self.generation = 0
        self.status = RuntimeStatus.STARTING
        self.halt_reason: str | None = None
        self.metrics = RuntimeMetrics()
        self.last_market_event_at: datetime | None = None
        self.last_reconcile_at: datetime | None = None
        self.last_degradation_at: datetime | None = None
        self.consecutive_failures = 0
        self.paper_valuations: list[dict[str, object]] = []
        self._started = False

    def start(self) -> RuntimeHealth:
        now = self._now()
        record = self.store.claim(self.runtime_id, self.mode.value, self.owner_id, now=now, lease_seconds=self.config.lease_seconds)
        self.generation = record.generation
        self._started = True
        checkpoint = None
        try:
            self.store.verify_v1_execution_journal(self.runtime_id)
            self.store.verify_v1_pre_dispatch_journal(self.runtime_id)
            self.store.verify_v1_external_action_journal(self.runtime_id)
            self.store.verify_v1_private_reconciliation_journal(self.runtime_id)
            self.store.verify_v1_private_drift_journal(self.runtime_id)
            self.store.verify_v1_reconciliation_watchdog_journal(self.runtime_id)
            checkpoint = self.store.latest_checkpoint(self.runtime_id)
            if checkpoint is not None:
                self._restore(checkpoint.payload)
                self.metrics.restarts += 1
        except Exception as exc:
            self.status = RuntimeStatus.HALTED
            self.halt_reason = f"STARTUP_RECOVERY_FAILURE:{type(exc).__name__}"
            self.store.set_status(self.runtime_id, self.owner_id, status=self.status.value, halt_reason=self.halt_reason, now=now)
            self.store.append_event(
                self.runtime_id,
                self.owner_id,
                event_type="STARTUP_RECOVERY_FAILURE",
                occurred_at=now,
                payload={"exception_type": type(exc).__name__},
            )
            raise
        if self.status != RuntimeStatus.HALTED:
            self.status = RuntimeStatus.RUNNING
            self.halt_reason = None
        self.store.set_status(self.runtime_id, self.owner_id, status=self.status.value, halt_reason=self.halt_reason, now=now)
        self._refresh_all_v1_reconciliation_watchdogs(at=now)
        self.store.append_event(
            self.runtime_id,
            self.owner_id,
            event_type="RUNTIME_RESTARTED" if checkpoint is not None else "RUNTIME_STARTED",
            occurred_at=now,
            payload={"generation": self.generation, "mode": self.mode.value, "status": self.status.value},
        )
        self.checkpoint()
        return self.health()

    def stop(self) -> None:
        if not self._started:
            return
        now = self._now()
        if self.status != RuntimeStatus.HALTED:
            self.status = RuntimeStatus.STOPPED
            self.store.set_status(self.runtime_id, self.owner_id, status=self.status.value, halt_reason=None, now=now)
        self.store.append_event(self.runtime_id, self.owner_id, event_type="RUNTIME_STOPPED", occurred_at=now, payload={"status": self.status.value})
        self.checkpoint()
        self.store.release(self.runtime_id, self.owner_id, now=now)
        self._started = False

    def heartbeat(self) -> RuntimeHealth:
        self._require_started()
        now = self._now()
        self.store.renew(self.runtime_id, self.owner_id, now=now, lease_seconds=self.config.lease_seconds)
        self.metrics.heartbeats += 1
        self._refresh_all_v1_reconciliation_watchdogs(at=now)
        self.store.append_event(self.runtime_id, self.owner_id, event_type="HEARTBEAT", occurred_at=now, payload={"status": self.status.value})
        self.checkpoint()
        return self.health()

    def submit_v1_order(
        self,
        order: OrderRequest,
        *,
        intent: OrderIntent,
        capability: ConnectionCapability,
        interlock: SubmissionInterlock,
        venue_adapter: CanonicalVenueAdapter,
        lineage: dict[str, Any] | None = None,
    ) -> object:
        """Execute through the existing PAPER/SHADOW path after frozen-V1 authority verification.

        The legacy engines remain the behavior authority. This method adds a durable V1
        authority binding and lifecycle journal around them; it never enables venue submission.
        """
        self._require_operational()
        now = self._now()
        if intent.asset_class == AssetClass.OPTION:
            self.store.append_event(
                self.runtime_id,
                self.owner_id,
                event_type="EQS00_EXECUTION_OPTIONS_DISABLED",
                occurred_at=now,
                payload={
                    "decision": "REJECT",
                    "code": FrozenWorkstreamExecutionRejected.code,
                    "workstream": "EQS-06",
                    "freeze_record": "EQS00-FRZ-EQS06-001",
                    "execution_intent_id": intent.execution_intent_id,
                    "client_order_id": str(order.order_id),
                    "external_action_permitted": False,
                    "retryable": False,
                    "resume_required": True,
                },
            )
            raise FrozenWorkstreamExecutionRejected()
        runtime_mode = ExecutionMode(self.mode.value)
        gateway_enabled = False
        if self.mode == RuntimeMode.SHADOW:
            assert self.shadow_engine is not None
            gateway_enabled = self.shadow_engine.gateway.venue_submission_enabled
        try:
            self._assert_v1_order_matches_legacy(order, intent)
            verification = verify_nonlive_execution_authority(
                intent,
                capability,
                interlock,
                runtime_mode=runtime_mode,
                at=now,
                gateway_submission_enabled=gateway_enabled,
            )
        except Exception as exc:
            self.store.append_event(
                self.runtime_id,
                self.owner_id,
                event_type="V1_AUTHORITY_REJECTED",
                occurred_at=now,
                payload={
                    "execution_intent_id": intent.execution_intent_id,
                    "client_order_id": str(order.order_id),
                    "exception_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            self.halt(f"V1_AUTHORITY_REJECTED:{type(exc).__name__}")
            raise

        try:
            self._assert_v1_trading_commissioning_clearance(capability, at=now)
            pre_dispatch = verify_v1_pre_dispatch(
                intent,
                capability,
                venue_adapter,
                at=now,
                state_max_age_seconds=self.config.v1_state_max_age_seconds,
            )
        except Exception as exc:
            self.store.append_event(
                self.runtime_id,
                self.owner_id,
                event_type="V1_PRE_DISPATCH_REJECTED",
                occurred_at=now,
                payload={
                    "execution_intent_id": intent.execution_intent_id,
                    "client_order_id": str(order.order_id),
                    "exception_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            self.halt(f"V1_PRE_DISPATCH_REJECTED:{type(exc).__name__}")
            raise

        binding = self.store.bind_v1_intent(
            self.runtime_id,
            self.owner_id,
            client_order_id=str(order.order_id),
            execution_intent_id=intent.execution_intent_id,
            mode=self.mode.value,
            recorded_at=now,
            intent=intent.to_payload(),
            capability=capability.to_payload(),
            interlock=interlock.to_payload(),
            authority_verification=verification.to_payload(),
        )
        self.store.append_event(
            self.runtime_id,
            self.owner_id,
            event_type="V1_AUTHORITY_VERIFIED",
            occurred_at=now,
            payload={
                **verification.to_payload(),
                "client_order_id": str(order.order_id),
                "intent_sha256": binding.intent_sha256,
                "capability_sha256": binding.capability_sha256,
                "interlock_sha256": binding.interlock_sha256,
                "authority_sha256": binding.authority_sha256,
            },
        )

        pre_dispatch_binding = self.store.bind_v1_pre_dispatch(
            self.runtime_id,
            self.owner_id,
            client_order_id=str(order.order_id),
            execution_intent_id=intent.execution_intent_id,
            recorded_at=now,
            verification=pre_dispatch.to_payload(),
        )
        self.store.append_event(
            self.runtime_id,
            self.owner_id,
            event_type="V1_PRE_DISPATCH_VERIFIED",
            occurred_at=now,
            payload={
                **pre_dispatch.to_payload(),
                "client_order_id": str(order.order_id),
                "verification_sha256": pre_dispatch_binding.sha256,
            },
        )

        attempt = order_intent_to_submission_attempt_event(
            intent, client_order_id=str(order.order_id), receive_timestamp=now
        )
        truth_source = (
            JournalTruthSource.SIMULATOR if self.mode == RuntimeMode.PAPER else JournalTruthSource.SHADOW_PREVIEW
        )
        self.store.append_v1_execution_event(
            self.runtime_id,
            self.owner_id,
            event_id=attempt.event_id,
            execution_intent_id=intent.execution_intent_id,
            client_order_id=str(order.order_id),
            runtime_mode=self.mode.value,
            truth_source=truth_source.value,
            authoritative_external_truth=False,
            occurred_at=now,
            event=attempt.to_payload(),
        )

        v1_lineage = dict(lineage or {})
        v1_lineage["execution_v1"] = {
            "execution_intent_id": intent.execution_intent_id,
            "connection_id": capability.connection_id,
            "capability_verification_ref": capability.verification_ref,
            "interlock_id": interlock.interlock_id,
            "interlock_revision": interlock.revision,
            "interlock_seal_hash": interlock.seal_hash,
            "constraint_snapshot_id": pre_dispatch.constraint_snapshot_id,
            "constraint_snapshot_hash": pre_dispatch.constraint_snapshot_hash,
            "venue_state_hash": pre_dispatch.venue_state_hash,
            "session_state_hash": pre_dispatch.session_state_hash,
        }
        result = self.submit_order(order, lineage=v1_lineage)
        if self.mode == RuntimeMode.SHADOW:
            self.store.append_event(
                self.runtime_id,
                self.owner_id,
                event_type="V1_SHADOW_ZERO_SUBMIT_CONFIRMED",
                occurred_at=self._now(),
                payload={
                    "execution_intent_id": intent.execution_intent_id,
                    "client_order_id": str(order.order_id),
                    "interlock_id": interlock.interlock_id,
                    "sent": False,
                    "authoritative_external_truth": False,
                },
            )
            self.checkpoint()
        return result

    def submit_order(self, order: OrderRequest, *, lineage: dict[str, Any] | None = None) -> object:
        self._require_operational()
        now = self._now()
        self._assert_nonlive_mode_boundary()

        def execute(boundary_lineage: dict[str, object]) -> object:
            effective_lineage = {**(lineage or {}), **boundary_lineage}
            if self._order_seen(order.order_id):
                self.store.append_event(
                    self.runtime_id,
                    self.owner_id,
                    event_type="DUPLICATE_ORDER_BLOCKED",
                    occurred_at=now,
                    payload={"order_id": str(order.order_id), "lineage": effective_lineage},
                )
                self.checkpoint()
                raise DuplicateOrderError(f"duplicate order blocked: {order.order_id}")
            self.metrics.orders_received += 1
            try:
                if self.mode == RuntimeMode.PAPER:
                    assert self.paper_engine is not None
                    self.paper_engine.submit(order)
                    result: object = order.order_id
                    event_payload = {"order": encode_order(order), "path": "PAPER_SIMULATOR", "lineage": effective_lineage}
                else:
                    assert self.shadow_engine is not None
                    self._guard_shadow_broker_boundary(now)
                    decision = self.shadow_engine.evaluate_order(order)
                    if decision.submission_result.sent:
                        raise RuntimeError("shadow no-submit invariant violated")
                    self.metrics.shadow_decisions += 1
                    result = decision
                    event_payload = {
                        "order": encode_order(order),
                        "path": "SHADOW_DISABLED_GATEWAY",
                        "sent": False,
                        "reason": decision.submission_result.reason,
                        "lineage": effective_lineage,
                    }
                self.consecutive_failures = 0
                self.store.append_event(self.runtime_id, self.owner_id, event_type="ORDER_EVALUATED", occurred_at=now, payload=event_payload)
                self.checkpoint()
                return result
            except Exception as exc:
                self._record_failure("ORDER_EVALUATION", exc, fatal=True)
                raise

        return self._execute_certified_cycle(now, execute)

    def on_bar(self, bar: BarEvent) -> tuple[object, ...]:
        self._require_operational()
        if self.mode != RuntimeMode.PAPER:
            raise RuntimeError("on_bar is only valid for PAPER runtime")
        self._assert_nonlive_mode_boundary()
        now = self._now()
        assert self.paper_engine is not None

        def execute(boundary_lineage: dict[str, object]) -> tuple[object, ...]:
            # Snapshot the last known-good in-memory state immediately before PAPER mutation.
            pre_operation_state = self._state_payload()
            try:
                fills = self.paper_engine.on_bar(bar)
                self.last_market_event_at = bar.timestamp
                self.metrics.paper_fills += len(fills)
                reconciliation = self.paper_engine.ledger.reconcile()
                self.metrics.reconciliations += 1
                self.last_reconcile_at = self._now()
                if not reconciliation.ok:
                    self.metrics.reconciliation_failures += 1
                    self.halt("PAPER_LEDGER_RECONCILIATION_FAILURE")
                v1_settlements = []
                for record in fills:
                    binding = self.store.get_v1_intent_binding(self.runtime_id, str(record.order.order_id))
                    if binding is None:
                        continue
                    intent_payload = binding.intent
                    venue_id = str(intent_payload["route"]["venue_id"])
                    fee_currency = str(intent_payload["instrument"]["currency"])
                    event = paper_fill_to_execution_event(
                        record,
                        execution_id=binding.execution_intent_id,
                        venue_id=venue_id,
                        fee_currency=fee_currency,
                    )
                    previously_filled, terminal_source = self._v1_settlement_progress(binding.execution_intent_id)
                    if terminal_source is not None:
                        raise RuntimeError("cannot settle PAPER fill after terminal reservation settlement")
                    settlement = execution_event_to_reservation_settlement(
                        event,
                        reservation_id=str(intent_payload["reservation"]["reservation_id"]),
                        original_quantity=Decimal(str(intent_payload["target"]["quantity"])),
                        previously_filled_quantity=previously_filled,
                    )
                    v1_settlements.append(dict(
                        event_id=event.event_id,
                        execution_intent_id=binding.execution_intent_id,
                        client_order_id=str(record.order.order_id),
                        runtime_mode=self.mode.value,
                        truth_source=JournalTruthSource.SIMULATOR.value,
                        authoritative_external_truth=False,
                        occurred_at=record.fill.timestamp,
                        event=event.to_payload(),
                        settlement_id=settlement.settlement_id,
                        reservation_id=settlement.reservation_id,
                        settlement=settlement.to_payload(),
                    ))
                self.consecutive_failures = 0
                commit_at = self._now()
                self.store.append_event_and_checkpoint(
                    self.runtime_id,
                    self.owner_id,
                    event_type="PAPER_BAR_PROCESSED",
                    occurred_at=commit_at,
                    event_payload={
                        "symbol": bar.symbol,
                        "bar_timestamp": bar.timestamp.isoformat(),
                        "fills": len(fills),
                        "ledger_reconciled": reconciliation.ok,
                        "atomic_recovery_commit": True,
                        "lineage": boundary_lineage,
                    },
                    generation=self.generation,
                    checkpoint_payload=self._state_payload(),
                    v1_settlements=v1_settlements,
                )
                return fills
            except Exception as exc:
                try:
                    durable = self.store.latest_checkpoint(self.runtime_id)
                    if durable is not None:
                        self._restore(durable.payload)
                    else:
                        self._restore(pre_operation_state)
                except Exception:
                    self._restore(pre_operation_state)
                self._record_failure("PAPER_BAR", exc, fatal=True)
                raise

        return self._execute_certified_cycle(now, execute)

    def journal_v1_terminal_event(self, event: ExecutionEvent) -> ReservationSettlement:
        """Journal an already-observed CANCELLED/REJECTED non-live outcome and settle its reservation.

        This method is deliberately journal-only: it never asks PAPER or SHADOW to cancel
        an order and never calls a venue adapter.  It binds a canonical terminal event that
        the existing non-live behavior has already produced/observed.
        """
        self._require_operational()
        if event.event_type not in {ExecutionEventType.CANCELLED, ExecutionEventType.REJECTED}:
            raise ValueError("terminal reservation settlement requires CANCELLED or REJECTED execution event")
        binding = self.store.get_v1_intent_binding(self.runtime_id, event.client_order_id)
        if binding is None:
            raise V1AuthorityError("terminal execution event has no V1 intent binding")
        if event.execution_id != binding.execution_intent_id:
            raise V1AuthorityError("terminal execution event does not match bound execution intent")
        venue_id = str(binding.intent["route"]["venue_id"])
        if event.venue_id != venue_id:
            raise V1AuthorityError("terminal execution event does not match bound venue")

        previously_filled, terminal_source = self._v1_settlement_progress(binding.execution_intent_id)
        if terminal_source is not None and terminal_source != event.event_id:
            raise RuntimeError("reservation already has a different terminal settlement")
        settlement = execution_event_to_reservation_settlement(
            event,
            reservation_id=str(binding.intent["reservation"]["reservation_id"]),
            original_quantity=Decimal(str(binding.intent["target"]["quantity"])),
            previously_filled_quantity=previously_filled,
        )
        truth_source = (
            JournalTruthSource.SIMULATOR if self.mode == RuntimeMode.PAPER else JournalTruthSource.SHADOW_PREVIEW
        )
        self.store.append_v1_execution_event_with_settlement(
            self.runtime_id,
            self.owner_id,
            event_id=event.event_id,
            execution_intent_id=binding.execution_intent_id,
            client_order_id=event.client_order_id,
            runtime_mode=self.mode.value,
            truth_source=truth_source.value,
            authoritative_external_truth=False,
            occurred_at=event.receive_timestamp,
            event=event.to_payload(),
            settlement_id=settlement.settlement_id,
            reservation_id=settlement.reservation_id,
            settlement=settlement.to_payload(),
        )
        self.checkpoint()
        return settlement

    def reconcile_v1_private_read(
        self,
        adapter: CanonicalVenueAdapter,
        expectation: PrivateReadExpectation,
    ) -> PrivateReadReconciliationReport:
        """Collect, reconcile and durably seal all five private account-truth domains.

        This is observation-only. It never invokes submit/cancel. Incomplete private truth is
        tolerated as evidence in the existing non-live runtime, but if the adapter advertises
        TRADING_CAPABLE the runtime halts and the capability cannot pass pre-dispatch until a
        fresh, complete MATCHED reconciliation is durably present.
        """
        self._require_operational()
        now = self._now()
        self._ensure_v1_reconciliation_policy(adapter.connection_capability, at=now)
        previous_reconciliation = self.store.latest_v1_private_reconciliation(
            self.runtime_id, venue_id=adapter.connection_capability.venue_id,
            connection_id=adapter.connection_capability.connection_id,
        )
        previous_drift = self.store.latest_v1_private_drift(
            self.runtime_id, venue_id=adapter.connection_capability.venue_id,
            connection_id=adapter.connection_capability.connection_id,
        )
        report = reconcile_private_read(
            adapter, expectation, at=now,
            max_age_seconds=self.config.v1_private_reconciliation_max_age_seconds,
        )
        reconciliation = report.reconciliation
        self.store.append_v1_private_reconciliation(
            self.runtime_id, self.owner_id,
            reconciliation_id=reconciliation.reconciliation_id,
            venue_id=reconciliation.venue_id,
            connection_id=reconciliation.connection_id,
            completed_at=report.completed_at,
            commissioning_ready=report.commissioning_ready,
            report=report.to_payload(),
        )
        drift = assess_private_read_drift(
            None if previous_reconciliation is None else previous_reconciliation.report,
            report,
            previous_drift=None if previous_drift is None else previous_drift.report,
            detected_at=now,
        )
        self.store.append_v1_private_drift(
            self.runtime_id, self.owner_id,
            drift_id=drift.drift_id, venue_id=drift.venue_id, connection_id=drift.connection_id,
            previous_reconciliation_id=drift.previous_reconciliation_id,
            current_reconciliation_id=drift.current_reconciliation_id, detected_at=drift.detected_at,
            status=drift.status.value, unresolved=drift.unresolved, report=drift.to_payload(),
        )
        self._refresh_v1_reconciliation_watchdog(adapter.connection_capability, at=now)
        self.metrics.reconciliations += 1
        self.last_reconcile_at = now
        if not report.commissioning_ready:
            self.metrics.reconciliation_failures += 1
        self.store.append_event(
            self.runtime_id, self.owner_id,
            event_type="V1_PRIVATE_RECONCILIATION_RESULT",
            occurred_at=now,
            payload={
                "reconciliation_id": reconciliation.reconciliation_id,
                "venue_id": reconciliation.venue_id,
                "connection_id": reconciliation.connection_id,
                "status": reconciliation.status.value,
                "commissioning_ready": report.commissioning_ready,
                "payload_hash": report.payload_hash,
                "domains": [item.to_payload() for item in report.domains],
            },
        )
        self.store.append_event(
            self.runtime_id, self.owner_id,
            event_type="V1_PRIVATE_RECONCILIATION_DRIFT",
            occurred_at=now,
            payload=drift.to_payload(),
        )
        if adapter.connection_capability.has(Capability.TRADING_CAPABLE) and not report.commissioning_ready:
            self.halt("V1_TRADING_COMMISSIONING_BLOCKED:PRIVATE_TRUTH_INCOMPLETE")
        self.checkpoint()
        return report

    def _assert_v1_trading_commissioning_clearance(
        self, capability: ConnectionCapability, *, at: datetime
    ) -> None:
        if not capability.has(Capability.TRADING_CAPABLE):
            return
        stored = self.store.latest_v1_private_reconciliation(
            self.runtime_id, venue_id=capability.venue_id, connection_id=capability.connection_id,
        )
        if stored is None:
            raise TradingCommissioningBlocked("TRADING_CAPABLE requires durable private-read reconciliation")
        policy = self.store.get_v1_reconciliation_policy(
            self.runtime_id, venue_id=capability.venue_id, connection_id=capability.connection_id,
        )
        if policy is None:
            raise TradingCommissioningBlocked("TRADING_CAPABLE requires deterministic reconciliation cadence policy")
        watchdog = self._refresh_v1_reconciliation_watchdog(capability, at=at)
        if watchdog is None or watchdog.commissioning_hold:
            if watchdog is None:
                reason = "UNKNOWN"
            else:
                recovery_phase = watchdog.recovery_phase.value
                reason = watchdog.status.value if recovery_phase == "NONE" else f"{watchdog.status.value}:{recovery_phase}"
            raise TradingCommissioningBlocked(f"TRADING_CAPABLE reconciliation watchdog hold:{reason}")
        if stored.completed_at > at or (at - stored.completed_at).total_seconds() > self.config.v1_private_reconciliation_max_age_seconds:
            raise TradingCommissioningBlocked("TRADING_CAPABLE private-read reconciliation is stale")
        canonical = stored.report.get("reconciliation", {})
        domains = stored.report.get("domains", [])
        required = {"ORDERS", "FILLS", "FEES", "BALANCES", "POSITIONS"}
        complete = {str(item.get("domain")) for item in domains if isinstance(item, dict) and str(item.get("state")) == "COMPLETE"}
        if not stored.commissioning_ready or str(canonical.get("status")) != "MATCHED" or complete != required:
            raise TradingCommissioningBlocked("TRADING_CAPABLE blocked by incomplete external account truth")
        drift = self.store.latest_v1_private_drift(
            self.runtime_id, venue_id=capability.venue_id, connection_id=capability.connection_id,
        )
        if drift is None or drift.current_reconciliation_id != stored.reconciliation_id:
            raise TradingCommissioningBlocked("TRADING_CAPABLE requires drift assessment for latest private reconciliation")
        if drift.unresolved:
            raise TradingCommissioningBlocked("TRADING_CAPABLE blocked by unresolved private-account drift")

    def _ensure_v1_reconciliation_policy(
        self, capability: ConnectionCapability, *, at: datetime
    ) -> ReconciliationCadencePolicy:
        stored = self.store.get_v1_reconciliation_policy(
            self.runtime_id, venue_id=capability.venue_id, connection_id=capability.connection_id,
        )
        if stored is not None:
            return ReconciliationCadencePolicy(
                policy_id=stored.policy_id, venue_id=stored.venue_id, connection_id=stored.connection_id,
                cadence_seconds=stored.cadence_seconds, maximum_age_seconds=stored.maximum_age_seconds,
                anchor_at=stored.anchor_at, trading_capable=stored.trading_capable,
            )
        policy = make_reconciliation_policy(
            venue_id=capability.venue_id, connection_id=capability.connection_id,
            cadence_seconds=self.config.v1_private_reconciliation_cadence_seconds,
            maximum_age_seconds=self.config.v1_private_reconciliation_watchdog_max_age_seconds,
            anchor_at=at, trading_capable=True,
        )
        self.store.bind_v1_reconciliation_policy(self.runtime_id, self.owner_id, policy=policy.to_payload())
        self.store.append_event(
            self.runtime_id, self.owner_id, event_type="V1_RECONCILIATION_CADENCE_BOUND", occurred_at=at,
            payload=policy.to_payload(),
        )
        return policy

    def _refresh_v1_reconciliation_watchdog(
        self, capability: ConnectionCapability, *, at: datetime
    ):
        stored_policy = self.store.get_v1_reconciliation_policy(
            self.runtime_id, venue_id=capability.venue_id, connection_id=capability.connection_id,
        )
        if stored_policy is None:
            return None
        policy = ReconciliationCadencePolicy(
            policy_id=stored_policy.policy_id, venue_id=stored_policy.venue_id, connection_id=stored_policy.connection_id,
            cadence_seconds=stored_policy.cadence_seconds, maximum_age_seconds=stored_policy.maximum_age_seconds,
            anchor_at=stored_policy.anchor_at, trading_capable=stored_policy.trading_capable,
        )
        reconciliation = self.store.latest_v1_private_reconciliation(
            self.runtime_id, venue_id=capability.venue_id, connection_id=capability.connection_id,
        )
        latest = self.store.latest_v1_reconciliation_watchdog(
            self.runtime_id, venue_id=capability.venue_id, connection_id=capability.connection_id,
        )
        previous_assessment = None
        for item in self.store.list_v1_reconciliation_watchdog(self.runtime_id):
            if item.venue_id == capability.venue_id and item.connection_id == capability.connection_id:
                previous_assessment = advance_recovery_context(previous_assessment, item.report)
        assessment = assess_reconciliation_watchdog(
            policy, assessed_at=at,
            last_reconciliation_id=None if reconciliation is None else reconciliation.reconciliation_id,
            last_reconciliation_completed_at=None if reconciliation is None else reconciliation.completed_at,
            last_reconciliation_complete=False if reconciliation is None else reconciliation.commissioning_ready,
            previous_assessment=previous_assessment,
        )
        if latest is None or latest.assessment_id != assessment.assessment_id:
            self.store.append_v1_reconciliation_watchdog(
                self.runtime_id, self.owner_id, generation=self.generation, assessment=assessment.to_payload(),
            )
            self.store.append_event(
                self.runtime_id, self.owner_id,
                event_type="V1_RECONCILIATION_WATCHDOG_HOLD" if assessment.commissioning_hold else "V1_RECONCILIATION_WATCHDOG",
                occurred_at=at, payload=assessment.to_payload(),
            )
        return assessment

    def _refresh_all_v1_reconciliation_watchdogs(self, *, at: datetime) -> None:
        for stored_policy in self.store.list_v1_reconciliation_policies(self.runtime_id):
            capability = ConnectionCapability(
                connection_id=stored_policy.connection_id, venue_id=stored_policy.venue_id,
                capabilities=(Capability.PUBLIC_ONLY, Capability.PRIVATE_READ_ONLY),
                verified_at=at, verification_ref="WATCHDOG_DURABLE_POLICY",
                production_submission_enabled=False,
            )
            self._refresh_v1_reconciliation_watchdog(capability, at=at)

    def run_v1_reconciliation_scheduler(
        self, adapter: CanonicalVenueAdapter, expectation: PrivateReadExpectation
    ) -> PrivateReadReconciliationReport | None:
        """Run one deterministic scheduler tick for a venue/connection scope.

        No background thread or broker side effect is introduced.  The tick is due exactly at
        anchor/last-completion + cadence.  Missed cadence and maximum age are separately sealed
        by the watchdog and block future TRADING_CAPABLE commissioning.
        """
        self._require_operational()
        now = self._now()
        policy = self._ensure_v1_reconciliation_policy(adapter.connection_capability, at=now)
        latest = self.store.latest_v1_private_reconciliation(
            self.runtime_id, venue_id=policy.venue_id, connection_id=policy.connection_id,
        )
        completed_at = None if latest is None else latest.completed_at
        if reconciliation_due(policy, at=now, last_reconciliation_completed_at=completed_at):
            return self.reconcile_v1_private_read(adapter, expectation)
        self._refresh_v1_reconciliation_watchdog(adapter.connection_capability, at=now)
        return None

    def reconcile_shadow(self, *, expected_open_order_ids: set, expected_positions: dict) -> object:
        self._require_operational()
        if self.mode != RuntimeMode.SHADOW:
            raise RuntimeError("shadow reconciliation requires SHADOW runtime")
        self._assert_nonlive_mode_boundary()
        assert self.shadow_engine is not None
        now = self._now()

        def execute(boundary_lineage: dict[str, object]) -> object:
            try:
                self._guard_shadow_broker_boundary(now)
                snapshot = self.shadow_engine.gateway.adapter.account_snapshot()
                result = self.reconciler.reconcile(
                    expected_open_order_ids=expected_open_order_ids,
                    expected_positions=expected_positions,
                    venue=snapshot,
                )
                self.metrics.reconciliations += 1
                self.last_reconcile_at = now
                if result.action == ReconciliationAction.HALT:
                    self.metrics.reconciliation_failures += 1
                    self.halt("SHADOW_RECONCILIATION_FAILURE:" + ",".join(result.reasons))
                self.store.append_event(
                    self.runtime_id,
                    self.owner_id,
                    event_type="SHADOW_RECONCILIATION",
                    occurred_at=now,
                    payload={
                        "action": result.action.value,
                        "reasons": list(result.reasons),
                        "unknown_orders": [str(value) for value in result.unknown_venue_orders],
                        "missing_orders": [str(value) for value in result.missing_venue_orders],
                        "position_differences": {key: str(value) for key, value in result.position_differences.items()},
                        "containment_action_sent": False,
                        "lineage": boundary_lineage,
                    },
                )
                bindings = self.store.list_v1_intent_bindings(self.runtime_id)
                if bindings:
                    scopes = {
                        (str(item.capability["venue_id"]), str(item.capability["connection_id"]))
                        for item in bindings
                    }
                    if len(scopes) != 1:
                        self.halt("V1_RECONCILIATION_SCOPE_AMBIGUOUS")
                        raise RuntimeError("V1 reconciliation cannot span multiple venue/connection scopes")
                    venue_id, connection_id = next(iter(scopes))
                    completed_at = self._now()
                    evidence_refs = tuple(sorted({item.authority_sha256 for item in bindings}))
                    v1_reconciliation = execution_reconciliation_to_v1(
                        result,
                        venue_snapshot=snapshot,
                        expected_open_order_ids=expected_open_order_ids,
                        venue_id=venue_id,
                        connection_id=connection_id,
                        started_at=now,
                        completed_at=completed_at,
                        evidence_refs=evidence_refs,
                    )
                    reconciliation_payload = v1_reconciliation.to_payload()
                    reconciliation_hash = payload_hash(reconciliation_payload)
                    self.store.append_event(
                        self.runtime_id,
                        self.owner_id,
                        event_type="V1_RECONCILIATION_RESULT",
                        occurred_at=completed_at,
                        payload=reconciliation_payload,
                    )
                    for binding in bindings:
                        observation = reconciliation_observation_to_execution_event(
                            execution_id=binding.execution_intent_id,
                            client_order_id=binding.client_order_id,
                            venue_id=venue_id,
                            reconciliation_id=v1_reconciliation.reconciliation_id,
                            reconciliation_payload_hash=reconciliation_hash,
                            receive_timestamp=completed_at,
                        )
                        self.store.append_v1_execution_event(
                            self.runtime_id,
                            self.owner_id,
                            event_id=observation.event_id,
                            execution_intent_id=binding.execution_intent_id,
                            client_order_id=binding.client_order_id,
                            runtime_mode=self.mode.value,
                            truth_source=JournalTruthSource.SHADOW_PREVIEW.value,
                            authoritative_external_truth=False,
                            occurred_at=completed_at,
                            event=observation.to_payload(),
                        )
                self.checkpoint()
                return result
            except Exception as exc:
                self._record_failure("SHADOW_RECONCILIATION", exc, fatal=True)
                raise

        return self._execute_certified_cycle(now, execute)

    def assess_degradation(
        self,
        expected: ExpectedBehavior,
        observed: ObservedBehavior,
        *,
        feature_psi: float = 0.0,
        prediction_psi: float = 0.0,
        execution_error: bool = False,
        data_integrity_error: bool = False,
    ) -> DegradationAssessment:
        self._require_operational()
        assessment = self.degradation_monitor.assess(
            expected,
            observed,
            feature_psi=feature_psi,
            prediction_psi=prediction_psi,
            execution_error=execution_error,
            data_integrity_error=data_integrity_error,
        )
        self.metrics.degradation_checks += 1
        self.last_degradation_at = self._now()
        if assessment.state == DegradationState.PAUSED:
            self.metrics.degradation_pauses += 1
            self.halt("DEGRADATION_PAUSE:" + ",".join(assessment.reasons))
        elif assessment.state in (DegradationState.WATCH, DegradationState.REDUCED):
            self.status = RuntimeStatus.DEGRADED
            self.store.set_status(self.runtime_id, self.owner_id, status=self.status.value, halt_reason=None, now=self._now())
        elif self.status == RuntimeStatus.DEGRADED:
            self.status = RuntimeStatus.RUNNING
            self.store.set_status(self.runtime_id, self.owner_id, status=self.status.value, halt_reason=None, now=self._now())
        self.store.append_event(
            self.runtime_id,
            self.owner_id,
            event_type="DEGRADATION_ASSESSMENT",
            occurred_at=self._now(),
            payload={
                "state": assessment.state.value,
                "allocation_multiplier": assessment.allocation_multiplier,
                "severity": int(assessment.severity),
                "reasons": list(assessment.reasons),
                "metrics": assessment.metrics,
            },
        )
        self.checkpoint()
        return assessment

    def record_pipeline_event(self, event_type: str, payload: dict[str, Any]) -> int:
        self._require_started()
        if not event_type.strip():
            raise ValueError("event_type is required")
        sequence = self.store.append_event(
            self.runtime_id,
            self.owner_id,
            event_type=event_type,
            occurred_at=self._now(),
            payload=payload,
        )
        self.checkpoint()
        return sequence

    def report_failure(self, component: str, exc: Exception, *, fatal: bool = False) -> RuntimeStatus:
        self._require_started()
        if not component.strip():
            raise ValueError("failure component is required")
        self._record_failure(component, exc, fatal=fatal)
        return self.status

    def halt(self, reason: str) -> None:
        if not reason.strip():
            raise ValueError("halt reason is required")
        self.status = RuntimeStatus.HALTED
        self.halt_reason = reason
        if self.shadow_engine is not None:
            self.shadow_engine.gateway.halt(reason)
        now = self._now()
        self.store.set_status(self.runtime_id, self.owner_id, status=self.status.value, halt_reason=reason, now=now)
        self.store.append_event(self.runtime_id, self.owner_id, event_type="RUNTIME_HALTED", occurred_at=now, payload={"reason": reason})

    def attempt_recovery(self, *, expected_open_order_ids: set | None = None, expected_positions: dict | None = None) -> bool:
        self._require_started()
        self.metrics.recovery_attempts += 1
        now = self._now()
        reasons: list[str] = []
        try:
            self.store.verify_events(self.runtime_id)
            self.store.verify_v1_execution_journal(self.runtime_id)
            self.store.verify_v1_pre_dispatch_journal(self.runtime_id)
            self.store.verify_v1_external_action_journal(self.runtime_id)
            self.store.verify_v1_private_reconciliation_journal(self.runtime_id)
            self.store.verify_v1_private_drift_journal(self.runtime_id)
            self.store.verify_v1_reconciliation_watchdog_journal(self.runtime_id)
            if self.mode == RuntimeMode.PAPER:
                assert self.paper_engine is not None
                if not self.paper_engine.ledger.reconcile().ok:
                    reasons.append("PAPER_LEDGER_RECONCILIATION_FAILURE")
            else:
                self._assert_shadow_disabled()
                assert self.shadow_engine is not None
                health = self.shadow_engine.gateway.adapter.health()
                if not health.healthy:
                    reasons.append("BROKER_UNHEALTHY:" + health.reason)
                if expected_open_order_ids is not None and expected_positions is not None and health.healthy:
                    result = self.reconciler.reconcile(
                        expected_open_order_ids=expected_open_order_ids,
                        expected_positions=expected_positions,
                        venue=self.shadow_engine.gateway.adapter.account_snapshot(),
                    )
                    if result.action == ReconciliationAction.HALT:
                        reasons.extend(result.reasons)
        except Exception as exc:
            reasons.append(f"RECOVERY_CHECK_EXCEPTION:{type(exc).__name__}")
        recovered = not reasons
        if recovered:
            self.status = RuntimeStatus.RUNNING
            self.halt_reason = None
            self.consecutive_failures = 0
            if self.shadow_engine is not None:
                self.shadow_engine.gateway.halted = False
                self.shadow_engine.gateway.halt_reason = None
            self.metrics.recovery_successes += 1
        else:
            self.status = RuntimeStatus.HALTED
            self.halt_reason = "RECOVERY_BLOCKED:" + ",".join(reasons)
        self.store.set_status(self.runtime_id, self.owner_id, status=self.status.value, halt_reason=self.halt_reason, now=now)
        self.store.append_event(
            self.runtime_id,
            self.owner_id,
            event_type="RECOVERY_ATTEMPT",
            occurred_at=now,
            payload={"recovered": recovered, "reasons": reasons or ["RECOVERY_CHECKS_CLEAR"]},
        )
        self.checkpoint()
        return recovered

    def checkpoint(self) -> None:
        self._require_started()
        self.store.save_checkpoint(
            self.runtime_id,
            self.owner_id,
            generation=self.generation,
            created_at=self._now(),
            payload=self._state_payload(),
        )

    def health(self) -> RuntimeHealth:
        return RuntimeHealth(
            runtime_id=self.runtime_id,
            mode=self.mode,
            status=self.status,
            halt_reason=self.halt_reason,
            generation=self.generation,
            owner_id=self.owner_id,
            metrics=asdict(self.metrics),
            last_market_event_at=self.last_market_event_at,
            last_reconcile_at=self.last_reconcile_at,
            last_degradation_at=self.last_degradation_at,
            consecutive_failures=self.consecutive_failures,
        )

    def _record_failure(self, component: str, exc: Exception, *, fatal: bool) -> None:
        self.metrics.errors += 1
        self.consecutive_failures += 1
        reason = f"{component}:{type(exc).__name__}"
        now = self._now()
        self.store.append_event(
            self.runtime_id,
            self.owner_id,
            event_type="RUNTIME_ERROR",
            occurred_at=now,
            payload={"component": component, "exception_type": type(exc).__name__, "fatal": fatal, "consecutive_failures": self.consecutive_failures},
        )
        if fatal or self.consecutive_failures >= self.config.max_consecutive_failures:
            self.halt("FAIL_CLOSED:" + reason)
        else:
            self.status = RuntimeStatus.DEGRADED
            self.store.set_status(self.runtime_id, self.owner_id, status=self.status.value, halt_reason=None, now=now)
        self.checkpoint()

    @staticmethod
    def _assert_v1_order_matches_legacy(order: OrderRequest, intent: OrderIntent) -> None:
        mismatches: list[str] = []
        if intent.instrument.venue_symbol != order.symbol:
            mismatches.append("symbol")
        if intent.target.side != order.side.value:
            mismatches.append("side")
        if intent.target.quantity != order.quantity:
            mismatches.append("quantity")
        if intent.target.notional != order.notional:
            mismatches.append("notional")
        if intent.order.order_type.value != order.order_type.value:
            mismatches.append("order_type")
        if intent.order.limit_price != order.limit_price:
            mismatches.append("limit_price")
        if intent.order.reduce_only != order.reduce_only:
            mismatches.append("reduce_only")
        if intent.decision_timestamp != order.decision_time:
            mismatches.append("decision_timestamp")
        if intent.strategy.strategy_id != str(order.strategy_id):
            mismatches.append("strategy_id")
        if mismatches:
            raise V1AuthorityError("V1 intent does not match legacy order: " + ",".join(mismatches))

    def _v1_settlement_progress(self, execution_id: str) -> tuple[Decimal, str | None]:
        settlements = self.store.list_v1_reservation_settlements_for_execution(self.runtime_id, execution_id)
        filled = Decimal("0")
        terminal_source: str | None = None
        for item in settlements:
            event = str(item.settlement["event"])
            if event in {"PARTIAL_FILL", "FILLED"}:
                filled += Decimal(str(item.settlement["filled_quantity"]))
            if event in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}:
                if terminal_source is not None and terminal_source != item.source_event_id:
                    raise RuntimeError("multiple terminal reservation settlements detected")
                terminal_source = item.source_event_id
        return filled, terminal_source

    def _order_seen(self, order_id: object) -> bool:
        if self.mode == RuntimeMode.PAPER:
            assert self.paper_engine is not None
            return self.paper_engine.simulator.has_seen_order(order_id)
        assert self.shadow_engine is not None
        return self.shadow_engine.has_seen_order(order_id)

    def _assert_shadow_disabled(self) -> None:
        assert self.shadow_engine is not None
        if self.shadow_engine.gateway.venue_submission_enabled:
            self.halt("SHADOW_SUBMISSION_INVARIANT_BREACH")
            raise RuntimeError("shadow runtime detected venue submission enabled")

    def _state_payload(self) -> dict[str, object]:
        engine_state: dict[str, object]
        if self.mode == RuntimeMode.PAPER:
            assert self.paper_engine is not None
            engine_state = {"paper": encode_paper_state(self.paper_engine.export_state())}
        else:
            assert self.shadow_engine is not None
            engine_state = {"shadow": encode_shadow_state(self.shadow_engine.export_state())}
        return {
            "schema_version": self.STATE_SCHEMA_VERSION,
            "runtime_id": self.runtime_id,
            "mode": self.mode.value,
            "status": self.status.value,
            "halt_reason": self.halt_reason,
            "metrics": asdict(self.metrics),
            "last_market_event_at": None if self.last_market_event_at is None else self.last_market_event_at.isoformat(),
            "last_reconcile_at": None if self.last_reconcile_at is None else self.last_reconcile_at.isoformat(),
            "last_degradation_at": None if self.last_degradation_at is None else self.last_degradation_at.isoformat(),
            "consecutive_failures": self.consecutive_failures,
            "paper_valuations": self.paper_valuations if self.mode == RuntimeMode.PAPER else [],
            **engine_state,
        }

    def _restore(self, payload: dict[str, object]) -> None:
        if int(payload.get("schema_version", -1)) != self.STATE_SCHEMA_VERSION:
            raise ValueError("unsupported runtime checkpoint schema")
        if payload.get("runtime_id") != self.runtime_id or payload.get("mode") != self.mode.value:
            raise ValueError("runtime checkpoint identity mismatch")
        self.status = RuntimeStatus(str(payload["status"]))
        self.halt_reason = None if payload.get("halt_reason") is None else str(payload["halt_reason"])
        metrics = dict(payload["metrics"])
        self.metrics = RuntimeMetrics(**{key: int(value) for key, value in metrics.items()})
        self.last_market_event_at = self._optional_time(payload.get("last_market_event_at"))
        self.last_reconcile_at = self._optional_time(payload.get("last_reconcile_at"))
        self.last_degradation_at = self._optional_time(payload.get("last_degradation_at"))
        self.consecutive_failures = int(payload.get("consecutive_failures", 0))
        self.paper_valuations = list(payload.get("paper_valuations", []))
        if self.mode == RuntimeMode.PAPER:
            assert self.paper_engine is not None
            self.paper_engine.restore_state(decode_paper_state(dict(payload["paper"])))
        else:
            self._assert_shadow_disabled()
            assert self.shadow_engine is not None
            self.shadow_engine.restore_state(decode_shadow_state(dict(payload["shadow"])))

    def _require_started(self) -> None:
        if not self._started:
            raise RuntimeError("runtime is not started")

    def _require_operational(self) -> None:
        self._require_started()
        if self.status in (RuntimeStatus.HALTED, RuntimeStatus.STOPPED):
            raise RuntimeError(f"runtime is not operational: {self.status.value}")
        self.store.renew(self.runtime_id, self.owner_id, now=self._now(), lease_seconds=self.config.lease_seconds)

    def submit_authorised_order(
        self,
        order: OrderRequest,
        authority: CapitalExecutionAuthorityV1,
        *,
        lineage: dict[str, Any] | None = None,
    ) -> object:
        """Capital-bound PAPER/SHADOW entry point. LIVE authority cannot be constructed."""
        if authority.execution_mode != self.mode.value:
            raise PermissionError("CAPITAL_AUTHORITY_MODE_MISMATCH")
        guarded = CapitalGuard.validate(authority, order, now=order.decision_time)
        pending = SubmissionLifecycle.before_submit(guarded)
        capital_lineage = {
            "capital_authority_schema": authority.schema_version,
            "reservation_id": str(authority.reservation_id),
            "risk_decision_id": str(authority.risk_decision_id),
            "mandate_id": authority.mandate_id,
            "mandate_version": authority.mandate_version,
            "authorised_quantity": str(authority.authorised_quantity),
            "reserved_notional": str(authority.reserved_notional),
            "execution_state_before_runtime": pending.state.value,
        }
        return self.submit_order(order, lineage={**(lineage or {}), **capital_lineage})

    def record_paper_valuation(self, marks: dict[str, object], *, observed_at: datetime, source: dict[str, object] | None = None) -> dict[str, object]:
        self._require_operational()
        if self.mode != RuntimeMode.PAPER:
            raise RuntimeError("paper valuation requires PAPER runtime")
        assert self.paper_engine is not None
        from decimal import Decimal
        clean = {str(k): Decimal(str(v)) for k, v in marks.items()}
        snap = self.paper_engine.snapshot(clean)
        row = {"observed_at": observed_at.isoformat(), "marks": {k: str(v) for k,v in clean.items()}, "cash": str(snap.cash), "equity": str(snap.equity), "realized_pnl": str(snap.realized_pnl), "unrealized_pnl": str(snap.unrealized_pnl), "commissions": str(snap.commissions), "financing_costs": str(snap.financing_costs), "source": source or {}}
        self.paper_valuations.append(row)
        self.paper_valuations = self.paper_valuations[-1000:]
        self.store.append_event(self.runtime_id,self.owner_id,event_type="PAPER_VALUATION",occurred_at=self._now(),payload=row)
        self.checkpoint()
        return row

    def reconcile_private_shadow(
        self,
        *,
        adapter: object,
        expected_open_order_ids: set,
        expected_positions: dict,
        max_age_seconds: float = 15.0,
    ) -> object:
        """Reconcile SHADOW expected state from an authenticated/read-only adapter."""
        self._require_operational()
        if self.mode != RuntimeMode.SHADOW:
            raise RuntimeError("private shadow reconciliation requires SHADOW runtime")
        self._assert_nonlive_mode_boundary()
        if getattr(adapter, "trading_capable", True):
            raise PermissionError("PRIVATE_RECONCILIATION_REQUIRES_READ_ONLY_ADAPTER")
        now = self._now()

        def execute(boundary_lineage: dict[str, object]) -> object:
            try:
                self._guard_shadow_broker_boundary(now)
                snapshot = adapter.private_read()
                result = PrivateStateReconciler(max_age_seconds=max_age_seconds).reconcile(
                    snapshot=snapshot,
                    expected_open_order_ids=set(expected_open_order_ids),
                    expected_positions=dict(expected_positions),
                    now=now,
                )
                self.metrics.reconciliations += 1
                self.last_reconcile_at = now
                if result.action == ReconciliationAction.HALT:
                    self.metrics.reconciliation_failures += 1
                    self.halt("SHADOW_PRIVATE_RECONCILIATION_FAILURE:" + ",".join(result.reasons))
                self.store.append_event(
                    self.runtime_id,
                    self.owner_id,
                    event_type="SHADOW_PRIVATE_RECONCILIATION",
                    occurred_at=now,
                    payload={
                        "action": result.action.value,
                        "reasons": list(result.reasons),
                        "snapshot_observed_at": result.observed_at.isoformat(),
                        "age_seconds": result.age_seconds,
                        "unknown_orders": [str(value) for value in result.unknown_venue_orders],
                        "missing_orders": [str(value) for value in result.missing_venue_orders],
                        "position_differences": {key: str(value) for key, value in result.position_differences.items()},
                        "adapter_venue": getattr(adapter, "venue", "UNKNOWN"),
                        "trading_capable": False,
                        "containment_action_sent": False,
                        "lineage": boundary_lineage,
                    },
                )
                self.checkpoint()
                return result
            except Exception as exc:
                self._record_failure("SHADOW_PRIVATE_RECONCILIATION", exc, fatal=True)
                raise

        return self._execute_certified_cycle(now, execute)

    def _assert_nonlive_mode_boundary(self) -> None:
        if self.mode not in (RuntimeMode.PAPER, RuntimeMode.SHADOW):
            self.halt("NONLIVE_MODE_BOUNDARY_BREACH")
            raise RuntimeError("runtime mode is outside PAPER/SHADOW boundary")
        if self.mode == RuntimeMode.SHADOW:
            self._assert_shadow_disabled()

    def _broker_submission_state(self) -> bool:
        if self.mode != RuntimeMode.SHADOW:
            return False
        assert self.shadow_engine is not None
        return bool(self.shadow_engine.gateway.venue_submission_enabled)

    def _record_operational_decision(self, now: datetime, decision: OperationalCertificationDecision) -> dict[str, object]:
        self.store.append_event(
            self.runtime_id,
            self.owner_id,
            event_type="OPERATIONAL_CERTIFICATION_DECISION",
            occurred_at=now,
            payload={
                "allowed": decision.allowed,
                "runtime_mode": decision.runtime_mode,
                "overall_outcome": decision.overall_outcome,
                "reason_codes": list(decision.reason_codes),
                "certificate_sha256": decision.certificate_sha256,
                "seal_sha256": decision.seal_sha256,
                "boundary_fingerprint": decision.boundary_fingerprint,
                "broker_submission_enabled": self._broker_submission_state(),
            },
        )
        return decision.lineage()

    def _execute_certified_cycle(self, now: datetime, action: Callable[[dict[str, object]], object]) -> object:
        if self.operational_boundary is None:
            return action({})
        recorded_fingerprints: set[str] = set()
        try:
            def certified_action(decision: OperationalCertificationDecision) -> object:
                lineage = self._record_operational_decision(now, decision)
                if decision.boundary_fingerprint is not None:
                    recorded_fingerprints.add(decision.boundary_fingerprint)
                return action(lineage)
            result, _ = self.operational_boundary.execute_certified_cycle(
                self.mode.value,
                broker_submission_state=self._broker_submission_state,
                certification_as_of=now,
                action=certified_action,
            )
            return result
        except OperationalCertificationBlocked as exc:
            decision = exc.decision
            if decision is not None and decision.boundary_fingerprint not in recorded_fingerprints:
                self._record_operational_decision(now, decision)
            reason = str(exc)
            if not reason.startswith("SHARED06_OPERATIONAL_BLOCK:"):
                reason = "SHARED06_OPERATIONAL_BLOCK:" + reason
            self.halt(reason)
            raise RuntimeError(reason) from exc

    def _guard_shadow_broker_boundary(self, now: datetime) -> None:
        self._assert_shadow_disabled()
        if self.operational_boundary is None:
            return
        try:
            self.operational_boundary.guard_broker_facing_read_or_evaluation(
                self.mode.value,
                broker_submission_state=self._broker_submission_state,
                certification_as_of=now,
            )
        except OperationalCertificationBlocked as exc:
            if exc.decision is not None:
                self._record_operational_decision(now, exc.decision)
            reason = str(exc)
            if not reason.startswith("SHARED06_OPERATIONAL_BLOCK:"):
                reason = "SHARED06_OPERATIONAL_BLOCK:" + reason
            self.halt(reason)
            raise RuntimeError(reason) from exc

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("runtime clock must return timezone-aware timestamps")
        return value

    @staticmethod
    def _optional_time(value: object | None) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("checkpoint timestamp must be timezone-aware")
        return parsed
