from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from quant_system.execution.models import OrderRequest


class VenueOrderStatus(StrEnum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class VenueOrder:
    client_order_id: UUID
    venue_order_id: str
    symbol: str
    status: VenueOrderStatus
    quantity: Decimal
    filled_quantity: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class VenueAccountSnapshot:
    open_orders: tuple[VenueOrder, ...]
    positions: dict[str, Decimal]
    observed_at: datetime

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class BrokerHealth:
    healthy: bool
    reason: str = "OK"


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    sent: bool
    client_order_id: UUID
    venue_order_id: str | None
    reason: str


class BrokerAdapter(Protocol):
    """Normalized venue contract. Implementations must not expose withdrawal actions."""

    def health(self) -> BrokerHealth: ...
    def submit_order(self, order: OrderRequest) -> VenueOrder: ...
    def cancel_order(self, client_order_id: UUID) -> None: ...
    def account_snapshot(self) -> VenueAccountSnapshot: ...


@dataclass(slots=True)
class InMemoryBrokerAdapter:
    """Deterministic broker double used for paper/staging acceptance only."""

    healthy: bool = True
    fail_submit: bool = False
    submitted: list[OrderRequest] = field(default_factory=list)
    canceled: list[UUID] = field(default_factory=list)
    open_orders: dict[UUID, VenueOrder] = field(default_factory=dict)
    positions: dict[str, Decimal] = field(default_factory=dict)

    def health(self) -> BrokerHealth:
        return BrokerHealth(self.healthy, "OK" if self.healthy else "INJECTED_BROKER_OUTAGE")

    def submit_order(self, order: OrderRequest) -> VenueOrder:
        if not self.healthy:
            raise ConnectionError("broker unavailable")
        if self.fail_submit:
            raise RuntimeError("injected submit failure")
        self.submitted.append(order)
        venue_order = VenueOrder(order.order_id, f"SIM-{len(self.submitted):08d}", order.symbol, VenueOrderStatus.ACKNOWLEDGED, order.quantity)
        self.open_orders[order.order_id] = venue_order
        return venue_order

    def cancel_order(self, client_order_id: UUID) -> None:
        if not self.healthy:
            raise ConnectionError("broker unavailable")
        self.canceled.append(client_order_id)
        self.open_orders.pop(client_order_id, None)

    def account_snapshot(self) -> VenueAccountSnapshot:
        if not self.healthy:
            raise ConnectionError("broker unavailable")
        return VenueAccountSnapshot(tuple(self.open_orders.values()), dict(self.positions), datetime.now(timezone.utc))


class BrokerGateway:
    """Submission boundary. Venue transmission is disabled unless explicitly enabled.

    F5 deliberately has no method that silently escalates this flag. F7 owns human live
    authorization and credential provisioning.
    """

    def __init__(self, adapter: BrokerAdapter, *, venue_submission_enabled: bool = False):
        self.adapter = adapter
        self.venue_submission_enabled = venue_submission_enabled
        self.halted = False
        self.halt_reason: str | None = None

    def halt(self, reason: str) -> None:
        self.halted = True
        self.halt_reason = reason

    def submit(self, order: OrderRequest, *, lineage: dict[str, object] | None = None) -> SubmissionResult:
        if lineage and lineage.get("infrastructure_boundary_fingerprint"):
            return SubmissionResult(False, order.order_id, None, "INFRASTRUCTURE_ONLY_DATASET_BLOCKED")
        if self.halted:
            return SubmissionResult(False, order.order_id, None, f"HALTED:{self.halt_reason}")
        health = self.adapter.health()
        if not health.healthy:
            self.halt(f"BROKER_UNHEALTHY:{health.reason}")
            return SubmissionResult(False, order.order_id, None, self.halt_reason or "BROKER_UNHEALTHY")
        if not self.venue_submission_enabled:
            return SubmissionResult(False, order.order_id, None, "VENUE_SUBMISSION_DISABLED")
        try:
            venue_order = self.adapter.submit_order(order)
        except Exception as exc:
            self.halt(f"SUBMISSION_FAILURE:{type(exc).__name__}")
            return SubmissionResult(False, order.order_id, None, self.halt_reason or "SUBMISSION_FAILURE")
        return SubmissionResult(True, order.order_id, venue_order.venue_order_id, "SENT")
