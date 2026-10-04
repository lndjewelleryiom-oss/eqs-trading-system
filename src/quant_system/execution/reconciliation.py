from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from .broker import VenueAccountSnapshot


class ReconciliationAction(StrEnum):
    OK = "OK"
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class ExecutionReconciliation:
    action: ReconciliationAction
    reasons: tuple[str, ...]
    unknown_venue_orders: tuple[UUID, ...]
    missing_venue_orders: tuple[UUID, ...]
    position_differences: dict[str, Decimal]


class ExecutionReconciler:
    """Fail-closed broker reconciliation with explicit unknown-order containment."""

    def reconcile(
        self,
        *,
        expected_open_order_ids: set[UUID],
        expected_positions: dict[str, Decimal],
        venue: VenueAccountSnapshot,
    ) -> ExecutionReconciliation:
        venue_ids = {order.client_order_id for order in venue.open_orders}
        unknown = tuple(sorted(venue_ids - expected_open_order_ids, key=str))
        missing = tuple(sorted(expected_open_order_ids - venue_ids, key=str))
        symbols = sorted(set(expected_positions) | set(venue.positions))
        differences = {
            symbol: venue.positions.get(symbol, Decimal("0")) - expected_positions.get(symbol, Decimal("0"))
            for symbol in symbols
        }
        differences = {symbol: value for symbol, value in differences.items() if value != 0}
        reasons: list[str] = []
        if unknown:
            reasons.append("UNKNOWN_VENUE_ORDER")
        if missing:
            reasons.append("MISSING_EXPECTED_OPEN_ORDER")
        if differences:
            reasons.append("POSITION_MISMATCH")
        action = ReconciliationAction.HALT if reasons else ReconciliationAction.OK
        return ExecutionReconciliation(action, tuple(reasons) or ("RECONCILED",), unknown, missing, differences)

    def contain_unknown_orders(self, reconciliation: ExecutionReconciliation, *, gateway) -> None:
        if reconciliation.action != ReconciliationAction.HALT:
            return
        gateway.halt("RECONCILIATION_FAILURE:" + ",".join(reconciliation.reasons))
        # Unknown venue orders are canceled only if communication is healthy. Failure to
        # cancel keeps the gateway halted and is surfaced to the caller.
        for client_order_id in reconciliation.unknown_venue_orders:
            gateway.adapter.cancel_order(client_order_id)
