from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Mapping
from uuid import UUID, uuid5

from quant_system.backtest.simulator import SimulatedFill
from quant_system.execution.broker import BrokerGateway, SubmissionResult, VenueAccountSnapshot, VenueOrder, VenueOrderStatus
from quant_system.execution.models import OrderRequest
from quant_system.execution.reconciliation import ExecutionReconciliation, ReconciliationAction
from quant_system.paper.engine import PaperFillRecord

from .models import (
    AssetClass,
    AuthorisationRef,
    Capability,
    ConnectionCapability,
    ExecutionEvent,
    ExecutionEventType,
    FeeDetail,
    FillDetail,
    ExecutionMode,
    InstrumentRef,
    InterlockIssuer,
    InterlockState,
    OrderIntent,
    OrderSpec,
    OrderTypeV1,
    ReconciliationCounts,
    ReconciliationResult,
    ReconciliationStatus,
    ReservationRef,
    ReservationSettlement,
    Route,
    SettlementEvent,
    StrategyRef,
    SubmissionInterlock,
    Target,
    TimeInForce,
    UnresolvedItem,
    UnresolvedKind,
)

_COMPAT_NAMESPACE = UUID("258f627c-542d-5af2-b7d8-ec7b3f281ef9")


class CompatibilityMappingError(ValueError):
    """Legacy state cannot be represented canonically without inventing information."""


@dataclass(frozen=True, slots=True)
class CryptoOrderIntentContext:
    portfolio_decision_id: str
    portfolio_id: str
    reservation_id: str
    risk_authorisation_id: str
    authorised_at: datetime
    expires_at: datetime
    venue_id: str
    connection_id: str
    required_capability: Capability
    mode: ExecutionMode
    programme_state_ref: str
    interlock_ref: str
    market_state_ref: str
    reference_data_ref: str
    instrument_id: str
    currency: str
    strategy_version: str
    time_in_force: TimeInForce
    campaign_id: str | None = None
    request_id: str | None = None
    underlying_id: str | None = None
    contract_id: str | None = None
    asset_extension: Mapping[str, object] | None = None


def _stable_id(prefix: str, payload: str) -> str:
    return f"{prefix}-{uuid5(_COMPAT_NAMESPACE, payload)}"


def order_request_to_order_intent(order: OrderRequest, context: CryptoOrderIntentContext) -> OrderIntent:
    """Map the current Crypto OrderRequest into the frozen V1 envelope.

    Missing portfolio/risk/reference authority is never guessed: it must be supplied by
    CryptoOrderIntentContext.  Existing OrderRequest.order_id is retained in the stable
    execution intent identity so retry/restart mappings remain deterministic.
    """
    request_id = context.request_id or _stable_id("REQ", str(order.order_id))
    execution_intent_id = _stable_id("EXEC", f"{order.order_id}|{context.portfolio_decision_id}")
    order_type = OrderTypeV1(order.order_type.value)
    return OrderIntent(
        execution_intent_id=execution_intent_id,
        request_id=request_id,
        portfolio_decision_id=context.portfolio_decision_id,
        strategy=StrategyRef(str(order.strategy_id), context.strategy_version, context.campaign_id),
        portfolio_id=context.portfolio_id,
        reservation=ReservationRef(context.reservation_id, context.risk_authorisation_id, context.authorised_at, context.expires_at),
        asset_class=AssetClass.CRYPTO,
        instrument=InstrumentRef(
            instrument_id=context.instrument_id,
            venue_symbol=order.symbol,
            underlying_id=context.underlying_id,
            currency=context.currency,
            contract_id=context.contract_id,
        ),
        target=Target(order.side.value, order.quantity, order.notional),
        order=OrderSpec(
            order_type=order_type,
            time_in_force=context.time_in_force,
            limit_price=order.limit_price,
            reduce_only=order.reduce_only,
        ),
        route=Route(context.venue_id, context.connection_id, context.required_capability),
        mode=context.mode,
        decision_timestamp=order.decision_time,
        authorisation=AuthorisationRef(context.programme_state_ref, context.interlock_ref),
        market_state_ref=context.market_state_ref,
        reference_data_ref=context.reference_data_ref,
        asset_extension=dict(context.asset_extension or {}),
    )


def connection_capability_from_gateway(
    gateway: BrokerGateway,
    *,
    connection_id: str,
    venue_id: str,
    verified_capabilities: Iterable[Capability],
    verified_at: datetime,
    verification_ref: str,
) -> ConnectionCapability:
    """Describe a legacy gateway without inferring permission from credentials/flags."""
    caps = tuple(dict.fromkeys(verified_capabilities))
    return ConnectionCapability(
        connection_id=connection_id,
        venue_id=venue_id,
        capabilities=caps,
        verified_at=verified_at,
        verification_ref=verification_ref,
        production_submission_enabled=gateway.venue_submission_enabled,
    )


def submission_interlock_from_gateway(
    gateway: BrokerGateway,
    *,
    mode: ExecutionMode,
    interlock_id: str,
    revision: int,
    issued_by: InterlockIssuer,
    valid_from: datetime,
    valid_until: datetime,
    programme_state_ref: str,
    seal_hash: str,
    allowed_strategy_ids: tuple[str, ...] = (),
    allowed_venue_ids: tuple[str, ...] = (),
    allowed_instrument_ids: tuple[str, ...] = (),
) -> SubmissionInterlock:
    """Represent the legacy physical kill state for non-LIVE operation only.

    A mutable legacy boolean is deliberately incapable of manufacturing LIVE_AUTHORISED.
    """
    if mode == ExecutionMode.LIVE:
        raise CompatibilityMappingError("LIVE_AUTHORISED cannot be derived from BrokerGateway.venue_submission_enabled")
    if gateway.venue_submission_enabled:
        raise CompatibilityMappingError(f"{mode.value} compatibility requires legacy venue submission disabled")
    state = InterlockState.PAPER_ONLY if mode == ExecutionMode.PAPER else InterlockState.SHADOW_ZERO_SUBMIT
    return SubmissionInterlock(
        interlock_id=interlock_id,
        revision=revision,
        state=state,
        issued_by=issued_by,
        valid_from=valid_from,
        valid_until=valid_until,
        programme_state_ref=programme_state_ref,
        seal_hash=seal_hash,
        allowed_strategy_ids=allowed_strategy_ids,
        allowed_venue_ids=allowed_venue_ids,
        allowed_instrument_ids=allowed_instrument_ids,
    )



def order_intent_to_submission_attempt_event(
    intent: OrderIntent,
    *,
    client_order_id: str,
    receive_timestamp: datetime,
    event_id: str | None = None,
    sequence: int | None = None,
) -> ExecutionEvent:
    eid = event_id or _stable_id(
        "EVT",
        f"{intent.execution_intent_id}|{client_order_id}|{ExecutionEventType.SUBMISSION_ATTEMPT.value}",
    )
    return ExecutionEvent.build(
        event_id=eid,
        execution_id=intent.execution_intent_id,
        client_order_id=client_order_id,
        venue_id=intent.route.venue_id,
        event_type=ExecutionEventType.SUBMISSION_ATTEMPT,
        receive_timestamp=receive_timestamp,
        sequence=sequence,
    )

def submission_result_to_execution_event(
    result: SubmissionResult,
    *,
    execution_id: str,
    venue_id: str,
    receive_timestamp: datetime,
    event_id: str | None = None,
    sequence: int | None = None,
) -> ExecutionEvent:
    reason = result.reason or ""
    if result.sent:
        event_type = ExecutionEventType.SUBMITTED
    elif reason.startswith("SUBMISSION_FAILURE"):
        event_type = ExecutionEventType.UNKNOWN_SUBMISSION_OUTCOME
    else:
        event_type = ExecutionEventType.SUBMISSION_ATTEMPT
    eid = event_id or _stable_id("EVT", f"{execution_id}|{result.client_order_id}|{event_type.value}|{sequence}")
    return ExecutionEvent.build(
        event_id=eid,
        execution_id=execution_id,
        client_order_id=str(result.client_order_id),
        external_order_id=result.venue_order_id,
        venue_id=venue_id,
        event_type=event_type,
        receive_timestamp=receive_timestamp,
        sequence=sequence,
    )


def venue_order_to_execution_event(
    order: VenueOrder,
    *,
    execution_id: str,
    venue_id: str,
    receive_timestamp: datetime,
    event_id: str | None = None,
    sequence: int | None = None,
    reject_code: str | None = None,
    reject_reason: str | None = None,
) -> ExecutionEvent:
    if order.status == VenueOrderStatus.ACKNOWLEDGED:
        event_type = ExecutionEventType.ACKNOWLEDGED
    elif order.status == VenueOrderStatus.CANCELED:
        event_type = ExecutionEventType.CANCELLED
    elif order.status == VenueOrderStatus.REJECTED:
        if not reject_code or not reject_reason:
            raise CompatibilityMappingError("legacy rejected VenueOrder lacks canonical reject code/reason")
        event_type = ExecutionEventType.REJECTED
    elif order.status in {VenueOrderStatus.PARTIAL, VenueOrderStatus.FILLED}:
        raise CompatibilityMappingError("legacy VenueOrder lacks fill identity and price; cannot fabricate canonical fill truth")
    else:
        raise CompatibilityMappingError(f"unsupported venue status: {order.status}")
    eid = event_id or _stable_id("EVT", f"{execution_id}|{order.client_order_id}|{order.venue_order_id}|{event_type.value}|{sequence}")
    return ExecutionEvent.build(
        event_id=eid,
        execution_id=execution_id,
        client_order_id=str(order.client_order_id),
        external_order_id=order.venue_order_id,
        venue_id=venue_id,
        event_type=event_type,
        receive_timestamp=receive_timestamp,
        sequence=sequence,
        reject_code=reject_code,
        reject_reason=reject_reason,
    )


def execution_reconciliation_to_v1(
    reconciliation: ExecutionReconciliation,
    *,
    venue_snapshot: VenueAccountSnapshot,
    expected_open_order_ids: set[UUID],
    venue_id: str,
    connection_id: str,
    started_at: datetime,
    completed_at: datetime,
    reconciliation_id: str | None = None,
    fills_seen: int = 0,
    evidence_refs: tuple[str, ...] = (),
) -> ReconciliationResult:
    unresolved: list[UnresolvedItem] = []
    unresolved.extend(UnresolvedItem(UnresolvedKind.ORDER, str(oid), True, "UNKNOWN_VENUE_ORDER") for oid in reconciliation.unknown_venue_orders)
    unresolved.extend(UnresolvedItem(UnresolvedKind.ORDER, str(oid), True, "MISSING_EXPECTED_OPEN_ORDER") for oid in reconciliation.missing_venue_orders)
    unresolved.extend(UnresolvedItem(UnresolvedKind.POSITION, symbol, True, f"POSITION_MISMATCH:{difference}") for symbol, difference in reconciliation.position_differences.items())
    status = ReconciliationStatus.MATCHED if reconciliation.action == ReconciliationAction.OK else ReconciliationStatus.RECONCILIATION_REQUIRED
    rid = reconciliation_id or _stable_id("RECON", f"{venue_id}|{connection_id}|{started_at.isoformat()}|{completed_at.isoformat()}")
    return ReconciliationResult(
        reconciliation_id=rid,
        venue_id=venue_id,
        connection_id=connection_id,
        started_at=started_at,
        completed_at=completed_at,
        status=status,
        counts=ReconciliationCounts(
            local_open_orders=len(expected_open_order_ids),
            external_open_orders=len(venue_snapshot.open_orders),
            fills_seen=fills_seen,
            position_mismatches=len(reconciliation.position_differences),
            unknown_orders=len(reconciliation.unknown_venue_orders),
        ),
        unresolved=tuple(unresolved),
        evidence_refs=evidence_refs,
    )




def reconciliation_observation_to_execution_event(
    *,
    execution_id: str,
    client_order_id: str,
    venue_id: str,
    reconciliation_id: str,
    reconciliation_payload_hash: str,
    receive_timestamp: datetime,
    event_id: str | None = None,
    sequence: int | None = None,
) -> ExecutionEvent:
    eid = event_id or _stable_id(
        "EVT",
        f"{execution_id}|{client_order_id}|{ExecutionEventType.RECONCILIATION_OBSERVATION.value}|{reconciliation_id}",
    )
    return ExecutionEvent.build(
        event_id=eid,
        execution_id=execution_id,
        client_order_id=client_order_id,
        venue_id=venue_id,
        event_type=ExecutionEventType.RECONCILIATION_OBSERVATION,
        receive_timestamp=receive_timestamp,
        sequence=sequence,
        raw_receipt_hash=reconciliation_payload_hash,
    )

def paper_fill_to_execution_event(
    record: PaperFillRecord,
    *,
    execution_id: str,
    venue_id: str,
    fee_currency: str,
    event_id: str | None = None,
    sequence: int | None = None,
) -> ExecutionEvent:
    """Represent a simulator fill in the frozen V1 event shape.

    The caller must persist it with truth_source=SIMULATOR and
    authoritative_external_truth=False; this adapter never promotes PAPER to venue truth.
    """
    fill: SimulatedFill = record.fill
    event_type = ExecutionEventType.FILL if fill.quantity == record.order.quantity else ExecutionEventType.PARTIAL_FILL
    fid = _stable_id(
        "FILL",
        f"{record.order.order_id}|{fill.timestamp}|{fill.quantity}|{fill.price}",
    )
    eid = event_id or _stable_id(
        "EVT",
        f"{execution_id}|{record.order.order_id}|{event_type.value}|{fill.timestamp}|{sequence}",
    )
    fees = ()
    if fill.commission != 0:
        fees = (FeeDetail("COMMISSION", fill.commission, fee_currency),)
    return ExecutionEvent.build(
        event_id=eid,
        execution_id=execution_id,
        client_order_id=str(record.order.order_id),
        venue_id=venue_id,
        event_type=event_type,
        receive_timestamp=fill.timestamp,
        venue_timestamp=None,
        sequence=sequence,
        fill=FillDetail(fid, fill.quantity, fill.price, "UNKNOWN"),
        fees=fees,
    )

def paper_fill_to_reservation_settlement(
    record: PaperFillRecord,
    *,
    reservation_id: str,
    execution_id: str,
    settlement_id: str | None = None,
    execution_truth_ref: str | None = None,
) -> ReservationSettlement:
    fill: SimulatedFill = record.fill
    remaining = record.order.quantity - fill.quantity
    if remaining < 0:
        raise CompatibilityMappingError("paper fill quantity exceeds order quantity")
    event = SettlementEvent.FILLED if remaining == 0 else SettlementEvent.PARTIAL_FILL
    sid = settlement_id or _stable_id("SETTLE", f"{reservation_id}|{execution_id}|{record.order.order_id}|{fill.timestamp}|{fill.quantity}|{fill.price}")
    return ReservationSettlement(
        settlement_id=sid,
        reservation_id=reservation_id,
        execution_id=execution_id,
        event=event,
        filled_quantity=fill.quantity,
        remaining_quantity=remaining,
        actual_notional=fill.quantity * fill.price,
        actual_fees=fill.commission,
        timestamp=fill.timestamp,
        execution_truth_ref=execution_truth_ref,
    )
