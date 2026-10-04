from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from quant_system.execution.adapter_contract import PrivateReadSnapshot
from quant_system.execution.broker import VenueAccountSnapshot
from quant_system.execution.reconciliation import (
    ExecutionReconciler,
    ExecutionReconciliation,
    ReconciliationAction,
)

@dataclass(frozen=True, slots=True)
class PrivateStateReconciliation:
    action: ReconciliationAction
    reasons: tuple[str, ...]
    observed_at: datetime
    age_seconds: float
    unknown_venue_orders: tuple[UUID, ...]
    missing_venue_orders: tuple[UUID, ...]
    position_differences: dict[str, Decimal]

class PrivateStateReconciler:
    """Read-only reconciliation. This class has no exchange mutation surface."""
    def __init__(self, *, max_age_seconds: float = 15.0):
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        self.max_age_seconds = max_age_seconds
        self._execution = ExecutionReconciler()
    def reconcile(
        self,
        *,
        snapshot: PrivateReadSnapshot,
        expected_open_order_ids: set[UUID],
        expected_positions: dict[str, Decimal],
        now: datetime,
    ) -> PrivateStateReconciliation:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        if snapshot.observed_at.tzinfo is None or snapshot.observed_at.utcoffset() is None:
            raise ValueError("snapshot observed_at must be timezone-aware")
        age = (now - snapshot.observed_at).total_seconds()
        venue = VenueAccountSnapshot(
            tuple(snapshot.open_orders),
            dict(snapshot.positions),
            snapshot.observed_at,
        )
        base = self._execution.reconcile(
            expected_open_order_ids=set(expected_open_order_ids),
            expected_positions=dict(expected_positions),
            venue=venue,
        )
        reasons = [] if base.action == ReconciliationAction.OK else list(base.reasons)
        if age < 0:
            reasons.append("PRIVATE_STATE_CLOCK_SKEW")
        elif age > self.max_age_seconds:
            reasons.append("PRIVATE_STATE_STALE")
        action = ReconciliationAction.HALT if reasons else ReconciliationAction.OK
        return PrivateStateReconciliation(
            action, tuple(reasons) or ("RECONCILED",), snapshot.observed_at, age,
            base.unknown_venue_orders, base.missing_venue_orders, base.position_differences,
        )
