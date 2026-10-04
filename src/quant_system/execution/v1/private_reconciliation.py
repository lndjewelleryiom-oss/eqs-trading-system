from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from types import MappingProxyType
from typing import Any, Mapping

from .models import (
    Capability,
    ConnectionCapability,
    ReconciliationCounts,
    ReconciliationResult,
    ReconciliationStatus,
    UnresolvedItem,
    UnresolvedKind,
)
from .venue_adapter import (
    AdapterResult,
    BalanceSnapshot,
    CanonicalVenueAdapter,
    CanonicalVenueFee,
    CanonicalVenueFill,
    CanonicalVenueOrder,
    PositionSnapshot,
)


class PrivateTruthDomain(StrEnum):
    ORDERS = "ORDERS"
    FILLS = "FILLS"
    FEES = "FEES"
    BALANCES = "BALANCES"
    POSITIONS = "POSITIONS"


class PrivateTruthState(StrEnum):
    COMPLETE = "COMPLETE"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    STALE = "STALE"


class TradingCommissioningBlocked(PermissionError):
    pass


def _wire(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple) or isinstance(value, frozenset):
        return [_wire(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _wire(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if hasattr(value, "__dataclass_fields__"):
        return {name: _wire(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def _hash(value: Any) -> str:
    raw = json.dumps(_wire(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class PrivateReadExpectation:
    open_order_ids: frozenset[str] = frozenset()
    fill_ids: frozenset[str] = frozenset()
    positions: Mapping[str, Decimal] = field(default_factory=dict)
    fees_by_currency: Mapping[str, Decimal] = field(default_factory=dict)
    balances_by_asset: Mapping[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "positions", MappingProxyType(dict(self.positions)))
        object.__setattr__(self, "fees_by_currency", MappingProxyType(dict(self.fees_by_currency)))
        object.__setattr__(self, "balances_by_asset", MappingProxyType(dict(self.balances_by_asset)))

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)


@dataclass(frozen=True, slots=True)
class PrivateTruthObservation:
    domain: PrivateTruthDomain
    state: PrivateTruthState
    observed_at: datetime
    count: int
    payload_hash: str | None
    error_category: str | None = None
    error_code: str | None = None
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)


@dataclass(frozen=True, slots=True)
class PrivateReadReconciliationReport:
    reconciliation: ReconciliationResult
    expectation: PrivateReadExpectation
    domains: tuple[PrivateTruthObservation, ...]
    orders: tuple[CanonicalVenueOrder, ...]
    fills: tuple[CanonicalVenueFill, ...]
    fees: tuple[CanonicalVenueFee, ...]
    balances: tuple[BalanceSnapshot, ...]
    positions: tuple[PositionSnapshot, ...]
    commissioning_ready: bool
    completed_at: datetime
    schema_version: str = field(default="EQS-EXEC-PRIVATE-RECON-v1.0", init=False)

    @property
    def payload_hash(self) -> str:
        return _hash(self._base_payload())

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "reconciliation": self.reconciliation.to_payload(),
            "expectation": self.expectation.to_payload(),
            "domains": [item.to_payload() for item in self.domains],
            "orders": [_wire(item) for item in self.orders],
            "fills": [item.to_payload() for item in self.fills],
            "fees": [item.to_payload() for item in self.fees],
            "balances": [item.to_payload() for item in self.balances],
            "positions": [_wire(item) for item in self.positions],
            "commissioning_ready": self.commissioning_ready,
            "completed_at": _wire(self.completed_at),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = self._base_payload()
        payload["payload_hash"] = self.payload_hash
        return payload


def _domain_observation(
    domain: PrivateTruthDomain,
    result: AdapterResult[Any],
    *,
    at: datetime,
    max_age_seconds: int,
) -> tuple[PrivateTruthObservation, Any | None]:
    if not result.ok:
        assert result.error is not None
        state = PrivateTruthState.UNSUPPORTED if result.error.category.value == "UNSUPPORTED" else PrivateTruthState.UNAVAILABLE
        return PrivateTruthObservation(
            domain=domain,
            state=state,
            observed_at=result.observed_at,
            count=0,
            payload_hash=None,
            error_category=result.error.category.value,
            error_code=result.error.code,
            reason=result.error.message,
        ), None
    if result.observed_at > at or (at - result.observed_at).total_seconds() > max_age_seconds:
        return PrivateTruthObservation(
            domain=domain,
            state=PrivateTruthState.STALE,
            observed_at=result.observed_at,
            count=0 if result.value is None else len(result.value),
            payload_hash=None if result.value is None else _hash(result.value),
            reason="private-read observation is stale or future-dated",
        ), None
    value = tuple(result.value or ())
    return PrivateTruthObservation(
        domain=domain,
        state=PrivateTruthState.COMPLETE,
        observed_at=result.observed_at,
        count=len(value),
        payload_hash=_hash(value),
    ), value


def reconcile_private_read(
    adapter: CanonicalVenueAdapter,
    expectation: PrivateReadExpectation,
    *,
    at: datetime,
    max_age_seconds: int = 30,
) -> PrivateReadReconciliationReport:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("private reconciliation timestamp must be timezone-aware")
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    capability = adapter.connection_capability
    if not (capability.has(Capability.PRIVATE_READ_ONLY) or capability.has(Capability.TRADING_CAPABLE)):
        raise PermissionError("private-read reconciliation requires PRIVATE_READ_ONLY or TRADING_CAPABLE capability")

    calls = (
        (PrivateTruthDomain.ORDERS, adapter.list_open_orders(at=at)),
        (PrivateTruthDomain.FILLS, adapter.list_recent_fills(at=at)),
        (PrivateTruthDomain.FEES, adapter.list_recent_fees(at=at)),
        (PrivateTruthDomain.BALANCES, adapter.get_balances(at=at)),
        (PrivateTruthDomain.POSITIONS, adapter.get_positions(at=at)),
    )
    completed_at = max([at, *(result.observed_at for _, result in calls)])
    observations: list[PrivateTruthObservation] = []
    values: dict[PrivateTruthDomain, tuple[Any, ...]] = {}
    unresolved: list[UnresolvedItem] = []
    kind_by_domain = {
        PrivateTruthDomain.ORDERS: UnresolvedKind.ORDER,
        PrivateTruthDomain.FILLS: UnresolvedKind.FILL,
        PrivateTruthDomain.FEES: UnresolvedKind.FEE,
        PrivateTruthDomain.BALANCES: UnresolvedKind.BALANCE,
        PrivateTruthDomain.POSITIONS: UnresolvedKind.POSITION,
    }
    for domain, result in calls:
        observation, value = _domain_observation(domain, result, at=completed_at, max_age_seconds=max_age_seconds)
        observations.append(observation)
        if value is None:
            unresolved.append(UnresolvedItem(
                kind=kind_by_domain[domain], reference=f"PRIVATE_TRUTH:{domain.value}", safety_critical=True,
                reason=f"{observation.state.value}:{observation.error_category or ''}:{observation.error_code or ''}:{observation.reason or ''}",
            ))
            values[domain] = ()
        else:
            values[domain] = value

    orders = tuple(values[PrivateTruthDomain.ORDERS])
    fills = tuple(values[PrivateTruthDomain.FILLS])
    fees = tuple(values[PrivateTruthDomain.FEES])
    balances = tuple(values[PrivateTruthDomain.BALANCES])
    positions = tuple(values[PrivateTruthDomain.POSITIONS])

    external_order_ids = {item.client_order_id for item in orders}
    unknown_orders = sorted(external_order_ids - set(expectation.open_order_ids))
    missing_orders = sorted(set(expectation.open_order_ids) - external_order_ids)
    for ref in unknown_orders:
        unresolved.append(UnresolvedItem(UnresolvedKind.ORDER, ref, True, "UNKNOWN_EXTERNAL_OPEN_ORDER"))
    for ref in missing_orders:
        unresolved.append(UnresolvedItem(UnresolvedKind.ORDER, ref, True, "MISSING_EXPECTED_OPEN_ORDER"))

    external_fill_ids = {item.fill_id for item in fills}
    for ref in sorted(external_fill_ids - set(expectation.fill_ids)):
        unresolved.append(UnresolvedItem(UnresolvedKind.FILL, ref, True, "UNKNOWN_EXTERNAL_FILL"))
    for ref in sorted(set(expectation.fill_ids) - external_fill_ids):
        unresolved.append(UnresolvedItem(UnresolvedKind.FILL, ref, True, "MISSING_EXPECTED_FILL"))

    external_positions = {item.instrument_id: item.quantity for item in positions}
    position_mismatches = 0
    for instrument_id in sorted(set(expectation.positions) | set(external_positions)):
        expected = expectation.positions.get(instrument_id, Decimal("0"))
        actual = external_positions.get(instrument_id, Decimal("0"))
        if expected != actual:
            position_mismatches += 1
            unresolved.append(UnresolvedItem(UnresolvedKind.POSITION, instrument_id, True, f"EXPECTED={expected};ACTUAL={actual}"))

    external_fees: dict[str, Decimal] = {}
    for item in fees:
        external_fees[item.currency] = external_fees.get(item.currency, Decimal("0")) + item.amount
    for currency in sorted(set(expectation.fees_by_currency) | set(external_fees)):
        expected = expectation.fees_by_currency.get(currency, Decimal("0"))
        actual = external_fees.get(currency, Decimal("0"))
        if expected != actual:
            unresolved.append(UnresolvedItem(UnresolvedKind.FEE, currency, True, f"EXPECTED={expected};ACTUAL={actual}"))

    external_balances = {item.asset: item.total for item in balances}
    for asset in sorted(set(expectation.balances_by_asset) | set(external_balances)):
        expected = expectation.balances_by_asset.get(asset, Decimal("0"))
        actual = external_balances.get(asset, Decimal("0"))
        if expected != actual:
            unresolved.append(UnresolvedItem(UnresolvedKind.BALANCE, asset, True, f"EXPECTED={expected};ACTUAL={actual}"))

    complete = all(item.state == PrivateTruthState.COMPLETE for item in observations)
    if not complete:
        status = ReconciliationStatus.BLOCKED_UNKNOWN
    elif unresolved:
        status = ReconciliationStatus.RECONCILIATION_REQUIRED
    else:
        status = ReconciliationStatus.MATCHED
    evidence_refs = tuple(dict.fromkeys(item.payload_hash for item in observations if item.payload_hash is not None))
    recon_id = "RECON-" + sha256(
        json.dumps({
            "venue_id": capability.venue_id,
            "connection_id": capability.connection_id,
            "at": at.isoformat(),
            "expectation": expectation.to_payload(),
            "domains": [item.to_payload() for item in observations],
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:48]
    reconciliation = ReconciliationResult(
        reconciliation_id=recon_id,
        venue_id=capability.venue_id,
        connection_id=capability.connection_id,
        started_at=at,
        completed_at=completed_at,
        status=status,
        counts=ReconciliationCounts(
            local_open_orders=len(expectation.open_order_ids),
            external_open_orders=len(orders),
            fills_seen=len(fills),
            position_mismatches=position_mismatches,
            unknown_orders=len(unknown_orders),
        ),
        unresolved=tuple(unresolved),
        evidence_refs=evidence_refs,
    )
    return PrivateReadReconciliationReport(
        reconciliation=reconciliation,
        expectation=expectation,
        domains=tuple(observations),
        orders=orders,
        fills=fills,
        fees=fees,
        balances=balances,
        positions=positions,
        commissioning_ready=complete and status == ReconciliationStatus.MATCHED,
        completed_at=completed_at,
    )


def assert_trading_capable_commissioning_ready(
    capability: ConnectionCapability,
    report: PrivateReadReconciliationReport,
    *,
    at: datetime,
    max_age_seconds: int = 30,
) -> None:
    if not capability.has(Capability.TRADING_CAPABLE):
        return
    if capability.venue_id != report.reconciliation.venue_id or capability.connection_id != report.reconciliation.connection_id:
        raise TradingCommissioningBlocked("private reconciliation scope does not match trading capability")
    if report.completed_at > at or (at - report.completed_at).total_seconds() > max_age_seconds:
        raise TradingCommissioningBlocked("private reconciliation evidence is stale")
    if not report.commissioning_ready:
        missing = [item.domain.value for item in report.domains if item.state != PrivateTruthState.COMPLETE]
        raise TradingCommissioningBlocked(
            "trading-capable commissioning blocked by incomplete or mismatched private truth"
            + (":" + ",".join(missing) if missing else "")
        )


__all__ = [
    "PrivateTruthDomain", "PrivateTruthState", "TradingCommissioningBlocked",
    "PrivateReadExpectation", "PrivateTruthObservation", "PrivateReadReconciliationReport",
    "reconcile_private_read", "assert_trading_capable_commissioning_ready",
]
