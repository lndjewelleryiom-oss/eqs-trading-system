from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from typing import Any

from .models import (
    ExecutionEvent,
    ExecutionEventType,
    OrderIntent,
    ReconciliationCounts,
    ReconciliationResult,
    ReconciliationStatus,
    UnresolvedItem,
    UnresolvedKind,
)


class ExternalActionProtocolError(RuntimeError):
    pass


class BlindExternalActionRetryError(ExternalActionProtocolError):
    pass


class ExternalActionKind(StrEnum):
    SUBMIT = "SUBMIT"
    CANCEL = "CANCEL"


class ExternalActionState(StrEnum):
    PREPARED = "PREPARED"
    DISPATCHING = "DISPATCHING"
    UNKNOWN_SUBMISSION_OUTCOME = "UNKNOWN_SUBMISSION_OUTCOME"
    UNKNOWN_CANCEL_OUTCOME = "UNKNOWN_CANCEL_OUTCOME"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    RESOLVED_ACKNOWLEDGED = "RESOLVED_ACKNOWLEDGED"
    RESOLVED_REJECTED = "RESOLVED_REJECTED"
    RESOLVED_ABSENT = "RESOLVED_ABSENT"
    RESOLVED_CANCELLED = "RESOLVED_CANCELLED"


class ExternalObservationStatus(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    REJECTED = "REJECTED"
    ABSENT = "ABSENT"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class ExternalActionObservation:
    status: ExternalObservationStatus
    observation_ref: str
    external_order_id: str | None = None
    reject_code: str | None = None
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        if len(self.observation_ref) < 8:
            raise ValueError("observation_ref must contain at least 8 characters")
        if self.status == ExternalObservationStatus.ACKNOWLEDGED and not self.external_order_id:
            raise ValueError("ACKNOWLEDGED observation requires external_order_id")
        if self.status == ExternalObservationStatus.CANCELLED and not self.external_order_id:
            raise ValueError("CANCELLED observation requires external_order_id")
        if self.status == ExternalObservationStatus.REJECTED and (not self.reject_code or not self.reject_reason):
            raise ValueError("REJECTED observation requires reject_code and reject_reason")


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")


def _stable_id(prefix: str, value: str, *, length: int = 48) -> str:
    return f"{prefix}-{sha256(value.encode('utf-8')).hexdigest()[:length]}"


def deterministic_external_action_id(
    *,
    execution_intent_id: str,
    client_order_id: str,
    action_kind: ExternalActionKind,
    attempt: int,
    parent_action_id: str | None = None,
) -> str:
    if attempt < 1:
        raise ValueError("attempt must be >= 1")
    return _stable_id(
        "ACT",
        f"{execution_intent_id}|{client_order_id}|{action_kind.value}|{attempt}|{parent_action_id or ''}",
    )


def _transition_id(action_id: str, state: ExternalActionState, discriminator: str) -> str:
    return _stable_id("TRN", f"{action_id}|{state.value}|{discriminator}")


def _event_id(action_id: str, event_type: ExecutionEventType, discriminator: str) -> str:
    return _stable_id("EVT", f"{action_id}|{event_type.value}|{discriminator}")


class SyntheticUnknownOutcomeProtocol:
    """Synthetic-only crash-boundary protocol for future broker-facing actions.

    It intentionally performs no network or BrokerGateway call.  Tests or certification
    harnesses may place an external side effect between begin_dispatch() and recovery,
    reproducing the exact crash/timeout ambiguity without enabling production transport.
    """

    EVIDENCE_CLASS = "SYNTHETIC_FIXTURE"

    def __init__(self, *, store: Any, runtime_id: str, owner_id: str):
        self.store = store
        self.runtime_id = runtime_id
        self.owner_id = owner_id

    def _binding(self, intent: OrderIntent, client_order_id: str) -> Any:
        binding = self.store.get_v1_intent_binding(self.runtime_id, client_order_id)
        if binding is None:
            raise ExternalActionProtocolError("external action requires an existing V1 intent binding")
        if binding.execution_intent_id != intent.execution_intent_id:
            raise ExternalActionProtocolError("external action intent does not match durable V1 binding")
        if str(binding.intent["route"]["venue_id"]) != intent.route.venue_id:
            raise ExternalActionProtocolError("external action venue does not match durable V1 binding")
        return binding

    def _append(
        self,
        *,
        action_id: str,
        action_kind: ExternalActionKind,
        attempt: int,
        execution_intent_id: str,
        client_order_id: str,
        parent_action_id: str | None,
        state: ExternalActionState,
        at: datetime,
        discriminator: str,
        payload: dict[str, Any],
    ) -> Any:
        _aware(at)
        return self.store.append_v1_external_action_transition(
            self.runtime_id,
            self.owner_id,
            transition_id=_transition_id(action_id, state, discriminator),
            action_id=action_id,
            action_kind=action_kind.value,
            attempt=attempt,
            execution_intent_id=execution_intent_id,
            client_order_id=client_order_id,
            parent_action_id=parent_action_id,
            state=state.value,
            occurred_at=at,
            evidence_class=self.EVIDENCE_CLASS,
            payload=payload,
        )

    def prepare_submit(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        at: datetime,
        attempt: int = 1,
        parent_action_id: str | None = None,
    ) -> Any:
        self._binding(intent, client_order_id)
        action_id = deterministic_external_action_id(
            execution_intent_id=intent.execution_intent_id,
            client_order_id=client_order_id,
            action_kind=ExternalActionKind.SUBMIT,
            attempt=attempt,
            parent_action_id=parent_action_id,
        )
        return self._append(
            action_id=action_id,
            action_kind=ExternalActionKind.SUBMIT,
            attempt=attempt,
            execution_intent_id=intent.execution_intent_id,
            client_order_id=client_order_id,
            parent_action_id=parent_action_id,
            state=ExternalActionState.PREPARED,
            at=at,
            discriminator="PREPARE",
            payload={
                "action_id": action_id,
                "action_kind": ExternalActionKind.SUBMIT.value,
                "attempt": attempt,
                "execution_intent_id": intent.execution_intent_id,
                "client_order_id": client_order_id,
                "venue_id": intent.route.venue_id,
                "parent_action_id": parent_action_id,
                "evidence_class": self.EVIDENCE_CLASS,
            },
        )

    def prepare_cancel(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        external_order_id: str,
        at: datetime,
        attempt: int = 1,
        parent_action_id: str | None = None,
    ) -> Any:
        if not external_order_id:
            raise ValueError("external_order_id is required for cancel")
        self._binding(intent, client_order_id)
        action_id = deterministic_external_action_id(
            execution_intent_id=intent.execution_intent_id,
            client_order_id=client_order_id,
            action_kind=ExternalActionKind.CANCEL,
            attempt=attempt,
            parent_action_id=parent_action_id,
        )
        return self._append(
            action_id=action_id,
            action_kind=ExternalActionKind.CANCEL,
            attempt=attempt,
            execution_intent_id=intent.execution_intent_id,
            client_order_id=client_order_id,
            parent_action_id=parent_action_id,
            state=ExternalActionState.PREPARED,
            at=at,
            discriminator=f"PREPARE|{external_order_id}",
            payload={
                "action_id": action_id,
                "action_kind": ExternalActionKind.CANCEL.value,
                "attempt": attempt,
                "execution_intent_id": intent.execution_intent_id,
                "client_order_id": client_order_id,
                "venue_id": intent.route.venue_id,
                "external_order_id": external_order_id,
                "parent_action_id": parent_action_id,
                "evidence_class": self.EVIDENCE_CLASS,
            },
        )

    def begin_dispatch(self, action_id: str, *, at: datetime) -> Any:
        latest = self.store.get_v1_external_action_latest(self.runtime_id, action_id)
        if latest is None:
            raise ExternalActionProtocolError("unknown external action")
        if latest.state != ExternalActionState.PREPARED.value:
            raise BlindExternalActionRetryError(
                f"blind external-action retry blocked: action={action_id} state={latest.state}"
            )
        return self._append(
            action_id=action_id,
            action_kind=ExternalActionKind(latest.action_kind),
            attempt=latest.attempt,
            execution_intent_id=latest.execution_intent_id,
            client_order_id=latest.client_order_id,
            parent_action_id=latest.parent_action_id,
            state=ExternalActionState.DISPATCHING,
            at=at,
            discriminator="DISPATCH",
            payload={
                **latest.payload,
                "dispatch_started_at": at.isoformat(),
                "side_effect_status": "UNKNOWN_UNTIL_RESPONSE_OR_RECONCILIATION",
            },
        )

    def _unknown_then_reconciliation_required(self, latest: Any, *, at: datetime, reason: str) -> Any:
        kind = ExternalActionKind(latest.action_kind)
        if kind == ExternalActionKind.SUBMIT:
            unknown_state = ExternalActionState.UNKNOWN_SUBMISSION_OUTCOME
            binding = self.store.get_v1_intent_binding(self.runtime_id, latest.client_order_id)
            assert binding is not None
            venue_id = str(binding.intent["route"]["venue_id"])
            receipt_hash = sha256(f"{self.EVIDENCE_CLASS}|{latest.action_id}|{reason}".encode("utf-8")).hexdigest()
            event = ExecutionEvent.build(
                event_id=_event_id(latest.action_id, ExecutionEventType.UNKNOWN_SUBMISSION_OUTCOME, reason),
                execution_id=latest.execution_intent_id,
                client_order_id=latest.client_order_id,
                venue_id=venue_id,
                event_type=ExecutionEventType.UNKNOWN_SUBMISSION_OUTCOME,
                receive_timestamp=at,
                raw_receipt_hash=receipt_hash,
            )
            unknown_payload: dict[str, Any] = {
                **latest.payload,
                "ambiguity_reason": reason,
                "execution_event": event.to_payload(),
            }
        else:
            unknown_state = ExternalActionState.UNKNOWN_CANCEL_OUTCOME
            unknown_payload = {
                **latest.payload,
                "ambiguity_reason": reason,
                "cancel_outcome": "UNKNOWN",
            }
        unknown = self._append(
            action_id=latest.action_id,
            action_kind=kind,
            attempt=latest.attempt,
            execution_intent_id=latest.execution_intent_id,
            client_order_id=latest.client_order_id,
            parent_action_id=latest.parent_action_id,
            state=unknown_state,
            at=at,
            discriminator=reason,
            payload=unknown_payload,
        )
        return self._append(
            action_id=unknown.action_id,
            action_kind=kind,
            attempt=unknown.attempt,
            execution_intent_id=unknown.execution_intent_id,
            client_order_id=unknown.client_order_id,
            parent_action_id=unknown.parent_action_id,
            state=ExternalActionState.RECONCILIATION_REQUIRED,
            at=at,
            discriminator=f"RECON_REQUIRED|{reason}",
            payload={
                **unknown.payload,
                "required_next_step": "RECONCILE_BY_DETERMINISTIC_ACTION_ID_BEFORE_ANY_RETRY",
            },
        )

    def mark_ambiguous(self, action_id: str, *, at: datetime, reason: str) -> Any:
        latest = self.store.get_v1_external_action_latest(self.runtime_id, action_id)
        if latest is None:
            raise ExternalActionProtocolError("unknown external action")
        if latest.state != ExternalActionState.DISPATCHING.value:
            raise ExternalActionProtocolError("ambiguous outcome can only follow DISPATCHING")
        return self._unknown_then_reconciliation_required(latest, at=at, reason=reason)

    def recover_after_restart(self, *, at: datetime) -> tuple[str, ...]:
        self.store.verify_v1_external_action_journal(self.runtime_id)
        recovered: list[str] = []
        for latest in self.store.list_v1_external_action_latest(self.runtime_id):
            if latest.state == ExternalActionState.DISPATCHING.value:
                self._unknown_then_reconciliation_required(
                    latest, at=at, reason="PROCESS_LOSS_AFTER_DISPATCH_BOUNDARY"
                )
                recovered.append(latest.action_id)
            elif latest.state in {
                ExternalActionState.UNKNOWN_SUBMISSION_OUTCOME.value,
                ExternalActionState.UNKNOWN_CANCEL_OUTCOME.value,
            }:
                kind = ExternalActionKind(latest.action_kind)
                self._append(
                    action_id=latest.action_id,
                    action_kind=kind,
                    attempt=latest.attempt,
                    execution_intent_id=latest.execution_intent_id,
                    client_order_id=latest.client_order_id,
                    parent_action_id=latest.parent_action_id,
                    state=ExternalActionState.RECONCILIATION_REQUIRED,
                    at=at,
                    discriminator="RESTART_RECON_REQUIRED",
                    payload={
                        **latest.payload,
                        "required_next_step": "RECONCILE_BY_DETERMINISTIC_ACTION_ID_BEFORE_ANY_RETRY",
                    },
                )
                recovered.append(latest.action_id)
        return tuple(recovered)

    def reconcile(self, action_id: str, observation: ExternalActionObservation, *, at: datetime) -> Any:
        latest = self.store.get_v1_external_action_latest(self.runtime_id, action_id)
        if latest is None:
            raise ExternalActionProtocolError("unknown external action")
        if latest.state != ExternalActionState.RECONCILIATION_REQUIRED.value:
            raise ExternalActionProtocolError("external action is not awaiting reconciliation")
        binding = self.store.get_v1_intent_binding(self.runtime_id, latest.client_order_id)
        if binding is None:
            raise ExternalActionProtocolError("external action lost its V1 intent binding")
        venue_id = str(binding.intent["route"]["venue_id"])
        connection_id = str(binding.capability["connection_id"])
        kind = ExternalActionKind(latest.action_kind)
        unresolved: tuple[UnresolvedItem, ...] = ()
        result_state: ExternalActionState | None = None
        execution_event: ExecutionEvent | None = None

        if observation.status == ExternalObservationStatus.UNKNOWN:
            unresolved = (
                UnresolvedItem(
                    UnresolvedKind.SUBMISSION_OUTCOME,
                    action_id,
                    True,
                    "EXTERNAL_ACTION_OUTCOME_STILL_UNKNOWN",
                ),
            )
        elif kind == ExternalActionKind.SUBMIT:
            if observation.status == ExternalObservationStatus.ACKNOWLEDGED:
                result_state = ExternalActionState.RESOLVED_ACKNOWLEDGED
                execution_event = ExecutionEvent.build(
                    event_id=_event_id(action_id, ExecutionEventType.ACKNOWLEDGED, observation.observation_ref),
                    execution_id=latest.execution_intent_id,
                    client_order_id=latest.client_order_id,
                    venue_id=venue_id,
                    external_order_id=observation.external_order_id,
                    event_type=ExecutionEventType.ACKNOWLEDGED,
                    receive_timestamp=at,
                )
            elif observation.status == ExternalObservationStatus.REJECTED:
                result_state = ExternalActionState.RESOLVED_REJECTED
                execution_event = ExecutionEvent.build(
                    event_id=_event_id(action_id, ExecutionEventType.REJECTED, observation.observation_ref),
                    execution_id=latest.execution_intent_id,
                    client_order_id=latest.client_order_id,
                    venue_id=venue_id,
                    event_type=ExecutionEventType.REJECTED,
                    receive_timestamp=at,
                    reject_code=observation.reject_code,
                    reject_reason=observation.reject_reason,
                )
            elif observation.status == ExternalObservationStatus.ABSENT:
                result_state = ExternalActionState.RESOLVED_ABSENT
            else:
                raise ExternalActionProtocolError("invalid reconciliation observation for SUBMIT")
        else:
            if observation.status == ExternalObservationStatus.CANCELLED:
                result_state = ExternalActionState.RESOLVED_CANCELLED
                execution_event = ExecutionEvent.build(
                    event_id=_event_id(action_id, ExecutionEventType.CANCELLED, observation.observation_ref),
                    execution_id=latest.execution_intent_id,
                    client_order_id=latest.client_order_id,
                    venue_id=venue_id,
                    external_order_id=observation.external_order_id,
                    event_type=ExecutionEventType.CANCELLED,
                    receive_timestamp=at,
                )
            elif observation.status == ExternalObservationStatus.REJECTED:
                result_state = ExternalActionState.RESOLVED_REJECTED
                execution_event = ExecutionEvent.build(
                    event_id=_event_id(action_id, ExecutionEventType.REJECTED, observation.observation_ref),
                    execution_id=latest.execution_intent_id,
                    client_order_id=latest.client_order_id,
                    venue_id=venue_id,
                    event_type=ExecutionEventType.REJECTED,
                    receive_timestamp=at,
                    reject_code=observation.reject_code,
                    reject_reason=observation.reject_reason,
                )
            elif observation.status == ExternalObservationStatus.ABSENT:
                unresolved = (
                    UnresolvedItem(
                        UnresolvedKind.ORDER,
                        action_id,
                        True,
                        "CANCEL_TARGET_ABSENT_WITHOUT_TERMINAL_TRUTH",
                    ),
                )
            else:
                raise ExternalActionProtocolError("invalid reconciliation observation for CANCEL")

        status = ReconciliationStatus.BLOCKED_UNKNOWN if unresolved else ReconciliationStatus.MATCHED
        reconciliation = ReconciliationResult(
            reconciliation_id=_stable_id("RECON", f"{action_id}|{observation.observation_ref}"),
            venue_id=venue_id,
            connection_id=connection_id,
            started_at=latest.occurred_at,
            completed_at=at,
            status=status,
            counts=ReconciliationCounts(
                local_open_orders=1,
                external_open_orders=1 if observation.status == ExternalObservationStatus.ACKNOWLEDGED else 0,
                fills_seen=0,
                position_mismatches=0,
                unknown_orders=1 if unresolved else 0,
            ),
            unresolved=unresolved,
            evidence_refs=(observation.observation_ref,),
        )
        next_state = result_state or ExternalActionState.RECONCILIATION_REQUIRED
        payload: dict[str, Any] = {
            **latest.payload,
            "observation": {
                "status": observation.status.value,
                "observation_ref": observation.observation_ref,
                "external_order_id": observation.external_order_id,
                "reject_code": observation.reject_code,
                "reject_reason": observation.reject_reason,
            },
            "reconciliation": reconciliation.to_payload(),
            "resolved": result_state is not None,
        }
        if execution_event is not None:
            payload["execution_event"] = execution_event.to_payload()
        return self._append(
            action_id=action_id,
            action_kind=kind,
            attempt=latest.attempt,
            execution_intent_id=latest.execution_intent_id,
            client_order_id=latest.client_order_id,
            parent_action_id=latest.parent_action_id,
            state=next_state,
            at=at,
            discriminator=f"RECON|{observation.observation_ref}|{observation.status.value}",
            payload=payload,
        )

    def prepare_retry_after_absence(self, action_id: str, intent: OrderIntent, *, at: datetime) -> Any:
        latest = self.store.get_v1_external_action_latest(self.runtime_id, action_id)
        if latest is None:
            raise ExternalActionProtocolError("unknown external action")
        if latest.action_kind != ExternalActionKind.SUBMIT.value or latest.state != ExternalActionState.RESOLVED_ABSENT.value:
            raise BlindExternalActionRetryError("retry requires reconciled RESOLVED_ABSENT submit action")
        return self.prepare_submit(
            intent,
            client_order_id=latest.client_order_id,
            at=at,
            attempt=latest.attempt + 1,
            parent_action_id=latest.action_id,
        )
