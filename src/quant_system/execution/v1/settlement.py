from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid5

from .models import ExecutionEvent, ExecutionEventType, ReservationSettlement, SettlementEvent

_SETTLEMENT_NAMESPACE = UUID("dad5f209-6b38-5d55-8ae4-655567642d15")


class ReservationSettlementError(ValueError):
    """An execution event cannot be settled without inventing or conflicting state."""


def _settlement_id(reservation_id: str, execution_id: str, event_id: str) -> str:
    return f"SETTLE-{uuid5(_SETTLEMENT_NAMESPACE, f'{reservation_id}|{execution_id}|{event_id}')}"


def execution_event_to_reservation_settlement(
    event: ExecutionEvent,
    *,
    reservation_id: str,
    original_quantity: Decimal,
    previously_filled_quantity: Decimal,
    settlement_id: str | None = None,
) -> ReservationSettlement:
    """Convert a canonical V1 execution outcome into a delta reservation settlement.

    Fill settlements contain only the quantity/notional/fees introduced by *this*
    execution event and the remaining quantity after it.  CANCELLED and REJECTED
    introduce no new fill economics and therefore carry zero filled/notional/fees
    while releasing the unfilled remainder.  Consumers can deterministically sum the
    ordered settlement stream without double-counting earlier partial fills.
    """
    if original_quantity <= 0:
        raise ReservationSettlementError("original_quantity must be positive")
    if previously_filled_quantity < 0 or previously_filled_quantity > original_quantity:
        raise ReservationSettlementError("previously_filled_quantity is outside the order quantity")

    mapping = {
        ExecutionEventType.PARTIAL_FILL: SettlementEvent.PARTIAL_FILL,
        ExecutionEventType.FILL: SettlementEvent.FILLED,
        ExecutionEventType.CANCELLED: SettlementEvent.CANCELLED,
        ExecutionEventType.REJECTED: SettlementEvent.REJECTED,
    }
    try:
        settlement_event = mapping[event.event_type]
    except KeyError as exc:
        raise ReservationSettlementError(
            f"execution event {event.event_type.value} does not produce a reservation settlement"
        ) from exc

    filled_quantity = Decimal("0")
    actual_notional = Decimal("0")
    actual_fees = sum((fee.amount for fee in event.fees), Decimal("0"))
    remaining_before = original_quantity - previously_filled_quantity

    if event.event_type in {ExecutionEventType.PARTIAL_FILL, ExecutionEventType.FILL}:
        if event.fill is None:  # model validation should already guarantee this
            raise ReservationSettlementError("fill settlement requires canonical fill detail")
        filled_quantity = event.fill.quantity
        if filled_quantity > remaining_before:
            raise ReservationSettlementError("fill quantity exceeds reservation remainder")
        remaining_quantity = remaining_before - filled_quantity
        actual_notional = filled_quantity * event.fill.price
        if event.event_type == ExecutionEventType.PARTIAL_FILL and remaining_quantity <= 0:
            raise ReservationSettlementError("PARTIAL_FILL cannot exhaust the reservation")
        if event.event_type == ExecutionEventType.FILL and remaining_quantity != 0:
            raise ReservationSettlementError("FILL must exhaust the reservation remainder")
    else:
        if remaining_before <= 0:
            raise ReservationSettlementError(f"{event.event_type.value} cannot settle an already filled reservation")
        remaining_quantity = remaining_before
        if event.fees:
            raise ReservationSettlementError(
                f"{event.event_type.value} settlement cannot attribute fees without a fill in V1"
            )

    return ReservationSettlement(
        settlement_id=settlement_id or _settlement_id(reservation_id, event.execution_id, event.event_id),
        reservation_id=reservation_id,
        execution_id=event.execution_id,
        event=settlement_event,
        filled_quantity=filled_quantity,
        remaining_quantity=remaining_quantity,
        actual_notional=actual_notional,
        actual_fees=actual_fees,
        timestamp=event.receive_timestamp,
        execution_truth_ref=event.event_id,
    )
