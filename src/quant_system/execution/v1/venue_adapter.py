from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Callable, Generic, Mapping, Protocol, TypeVar
from types import MappingProxyType
from uuid import UUID, uuid5

from quant_system.core.enums import OrderType, Side
from quant_system.data.crypto_perps.models import PerpetualInstrumentDefinition
from quant_system.execution.broker import BrokerAdapter, BrokerHealth, VenueOrder, VenueOrderStatus
from quant_system.execution.models import OrderRequest

from .models import AssetClass, Capability, ConnectionCapability, OrderIntent, OrderTypeV1

T = TypeVar("T")
_ADAPTER_NAMESPACE = UUID("e06aa092-ac67-57d0-9067-d54c2cab6191")


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _positive(value: Decimal | None, name: str) -> None:
    if value is not None and value <= 0:
        raise ValueError(f"{name} must be positive")


def _canonical(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, tuple):
        return [_canonical(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if hasattr(value, "__dataclass_fields__"):
        return {name: _canonical(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def _digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return sha256(raw).hexdigest()


def _multiple(value: Decimal, step: Decimal) -> bool:
    if step <= 0:
        return False
    return value % step == 0


class AdapterErrorCategory(StrEnum):
    AUTHENTICATION = "AUTHENTICATION"
    PERMISSION = "PERMISSION"
    RATE_LIMIT = "RATE_LIMIT"
    TRANSPORT = "TRANSPORT"
    VENUE_REJECT = "VENUE_REJECT"
    NOT_FOUND = "NOT_FOUND"
    UNSUPPORTED = "UNSUPPORTED"
    STALE_STATE = "STALE_STATE"
    INVALID_REQUEST = "INVALID_REQUEST"
    CONSTRAINT_VIOLATION = "CONSTRAINT_VIOLATION"
    TRADING_DISABLED = "TRADING_DISABLED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class AdapterError:
    category: AdapterErrorCategory
    code: str
    message: str
    retryable: bool = False
    retry_after_seconds: Decimal | None = None
    external_code: str | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("adapter error code/message are required")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds cannot be negative")


@dataclass(frozen=True, slots=True)
class AdapterResult(Generic[T]):
    value: T | None
    error: AdapterError | None
    observed_at: datetime

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        if (self.value is None) == (self.error is None):
            raise ValueError("adapter result must contain exactly one of value/error")

    @property
    def ok(self) -> bool:
        return self.error is None

    @classmethod
    def success(cls, value: T, observed_at: datetime) -> "AdapterResult[T]":
        return cls(value=value, error=None, observed_at=observed_at)

    @classmethod
    def failure(cls, error: AdapterError, observed_at: datetime) -> "AdapterResult[T]":
        return cls(value=None, error=error, observed_at=observed_at)


class VenueOperationalState(StrEnum):
    TRADING = "TRADING"
    DEGRADED = "DEGRADED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


class SessionState(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    HALTED = "HALTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class VenueState:
    venue_id: str
    state: VenueOperationalState
    observed_at: datetime
    reason: str = "OK"

    def __post_init__(self) -> None:
        if not self.venue_id:
            raise ValueError("venue_id is required")
        _aware(self.observed_at, "observed_at")

    @property
    def payload_hash(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return _canonical(self)

    def assert_fresh(self, at: datetime, *, max_age_seconds: int) -> None:
        _aware(at, "at")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if self.observed_at > at:
            raise ValueError("venue state was not yet observable")
        if (at - self.observed_at).total_seconds() > max_age_seconds:
            raise ValueError("venue state is stale")


@dataclass(frozen=True, slots=True)
class MarketSessionState:
    venue_id: str
    instrument_id: str
    state: SessionState
    observed_at: datetime
    session_label: str
    next_transition_at: datetime | None = None

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")
        if self.next_transition_at is not None:
            _aware(self.next_transition_at, "next_transition_at")
        if not self.venue_id or not self.instrument_id or not self.session_label:
            raise ValueError("venue_id, instrument_id and session_label are required")

    @property
    def payload_hash(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return _canonical(self)

    def assert_fresh(self, at: datetime, *, max_age_seconds: int) -> None:
        _aware(at, "at")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if self.observed_at > at:
            raise ValueError("market session state was not yet observable")
        if (at - self.observed_at).total_seconds() > max_age_seconds:
            raise ValueError("market session state is stale")
        if self.next_transition_at is not None and self.next_transition_at <= at:
            raise ValueError("market session state is past its advertised transition")


@dataclass(frozen=True, slots=True)
class ConstraintSnapshot:
    snapshot_id: str
    asset_class: AssetClass
    instrument_id: str
    venue_id: str
    venue_symbol: str
    observed_at: datetime
    valid_until: datetime
    source_ref: str
    source_version: str
    status: str
    tick_size: Decimal
    lot_size: Decimal
    min_quantity: Decimal | None = None
    max_quantity: Decimal | None = None
    min_notional: Decimal | None = None
    max_notional: Decimal | None = None
    min_price: Decimal | None = None
    max_price: Decimal | None = None
    asset_extension: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = field(default="EQS-EXEC-CONSTRAINT-SNAPSHOT-v1.0", init=False)

    def __post_init__(self) -> None:
        for name in ("snapshot_id", "instrument_id", "venue_id", "venue_symbol", "source_ref", "source_version", "status"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        _aware(self.observed_at, "observed_at")
        _aware(self.valid_until, "valid_until")
        if self.valid_until <= self.observed_at:
            raise ValueError("valid_until must be after observed_at")
        _positive(self.tick_size, "tick_size")
        _positive(self.lot_size, "lot_size")
        for name in ("min_quantity", "max_quantity", "min_notional", "max_notional", "min_price", "max_price"):
            _positive(getattr(self, name), name)
        if self.min_quantity is not None and self.max_quantity is not None and self.min_quantity > self.max_quantity:
            raise ValueError("min_quantity cannot exceed max_quantity")
        if self.min_notional is not None and self.max_notional is not None and self.min_notional > self.max_notional:
            raise ValueError("min_notional cannot exceed max_notional")
        if self.min_price is not None and self.max_price is not None and self.min_price > self.max_price:
            raise ValueError("min_price cannot exceed max_price")
        if self.asset_class == AssetClass.CRYPTO and not self.asset_extension.get("product_type"):
            raise ValueError("CRYPTO constraint snapshot requires product_type")
        object.__setattr__(self, "asset_extension", MappingProxyType(dict(self.asset_extension)))

    @property
    def payload_hash(self) -> str:
        return _digest(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return _canonical(self)

    def assert_fresh(self, at: datetime) -> None:
        _aware(at, "at")
        if at < self.observed_at:
            raise ValueError("constraint snapshot was not yet observable")
        if at > self.valid_until:
            raise ValueError("constraint snapshot is stale")


@dataclass(frozen=True, slots=True)
class ConstraintValidation:
    accepted: bool
    snapshot_id: str
    snapshot_hash: str
    checked_at: datetime
    violations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _aware(self.checked_at, "checked_at")
        if self.accepted == bool(self.violations):
            raise ValueError("accepted must be true only when there are no violations")


@dataclass(frozen=True, slots=True)
class CanonicalVenueOrder:
    client_order_id: str
    external_order_id: str
    venue_symbol: str
    status: str
    quantity: Decimal
    filled_quantity: Decimal


@dataclass(frozen=True, slots=True)
class PositionSnapshot:
    instrument_id: str
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class CanonicalVenueFill:
    fill_id: str
    client_order_id: str
    external_order_id: str | None
    instrument_id: str
    quantity: Decimal
    price: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.fill_id or not self.client_order_id or not self.instrument_id:
            raise ValueError("fill_id, client_order_id and instrument_id are required")
        _positive(self.quantity, "fill quantity")
        _positive(self.price, "fill price")
        _aware(self.observed_at, "observed_at")

    def to_payload(self) -> dict[str, Any]:
        return _canonical(self)


@dataclass(frozen=True, slots=True)
class CanonicalVenueFee:
    fee_id: str
    currency: str
    amount: Decimal
    observed_at: datetime
    fee_type: str = "OTHER"
    fill_id: str | None = None
    client_order_id: str | None = None

    def __post_init__(self) -> None:
        if not self.fee_id or not self.currency:
            raise ValueError("fee_id and currency are required")
        _aware(self.observed_at, "observed_at")

    def to_payload(self) -> dict[str, Any]:
        return _canonical(self)


@dataclass(frozen=True, slots=True)
class BalanceSnapshot:
    asset: str
    total: Decimal
    available: Decimal
    reserved: Decimal
    observed_at: datetime

    def __post_init__(self) -> None:
        if not self.asset:
            raise ValueError("balance asset is required")
        if self.total < 0 or self.available < 0 or self.reserved < 0:
            raise ValueError("balances cannot be negative")
        if self.available + self.reserved != self.total:
            raise ValueError("available + reserved must equal total")
        _aware(self.observed_at, "observed_at")

    def to_payload(self) -> dict[str, Any]:
        return _canonical(self)


@dataclass(frozen=True, slots=True)
class AccountStateSnapshot:
    venue_id: str
    connection_id: str
    observed_at: datetime
    positions: tuple[PositionSnapshot, ...]
    open_orders: tuple[CanonicalVenueOrder, ...]

    def __post_init__(self) -> None:
        _aware(self.observed_at, "observed_at")


class CanonicalVenueAdapter(Protocol):
    """Cross-asset V1 venue surface. Strategies must never call this interface directly."""

    connection_capability: ConnectionCapability

    def get_instrument_constraints(self, instrument_id: str, *, at: datetime) -> AdapterResult[ConstraintSnapshot]: ...
    def get_venue_state(self, *, at: datetime) -> AdapterResult[VenueState]: ...
    def get_market_session_state(self, instrument_id: str, *, at: datetime) -> AdapterResult[MarketSessionState]: ...
    def get_account_state(self, *, at: datetime) -> AdapterResult[AccountStateSnapshot]: ...
    def list_open_orders(self, *, at: datetime) -> AdapterResult[tuple[CanonicalVenueOrder, ...]]: ...
    def list_recent_fills(self, *, at: datetime) -> AdapterResult[tuple[CanonicalVenueFill, ...]]: ...
    def list_recent_fees(self, *, at: datetime) -> AdapterResult[tuple[CanonicalVenueFee, ...]]: ...
    def get_balances(self, *, at: datetime) -> AdapterResult[tuple[BalanceSnapshot, ...]]: ...
    def get_positions(self, *, at: datetime) -> AdapterResult[tuple[PositionSnapshot, ...]]: ...
    def get_order(self, client_order_id: str, *, at: datetime) -> AdapterResult[CanonicalVenueOrder]: ...
    def submit_order(self, intent: OrderIntent, *, action_id: str, at: datetime) -> AdapterResult[CanonicalVenueOrder]: ...
    def cancel_order(self, client_order_id: str, *, action_id: str, at: datetime) -> AdapterResult[bool]: ...


def validate_order_intent_against_constraints(
    intent: OrderIntent,
    snapshot: ConstraintSnapshot,
    *,
    checked_at: datetime,
) -> ConstraintValidation:
    violations: list[str] = []
    try:
        snapshot.assert_fresh(checked_at)
    except ValueError as exc:
        violations.append(str(exc).upper().replace(" ", "_"))
    if intent.asset_class != snapshot.asset_class:
        violations.append("ASSET_CLASS_MISMATCH")
    if intent.instrument.instrument_id != snapshot.instrument_id:
        violations.append("INSTRUMENT_ID_MISMATCH")
    if intent.route.venue_id != snapshot.venue_id:
        violations.append("VENUE_ID_MISMATCH")
    if intent.instrument.venue_symbol and intent.instrument.venue_symbol != snapshot.venue_symbol:
        violations.append("VENUE_SYMBOL_MISMATCH")
    if snapshot.status.upper() not in {"TRADING", "OPEN", "ACTIVE", "LIVE"}:
        violations.append("INSTRUMENT_NOT_TRADING")
    qty = intent.target.quantity
    if not _multiple(qty, snapshot.lot_size):
        violations.append("QUANTITY_NOT_LOT_ALIGNED")
    if snapshot.min_quantity is not None and qty < snapshot.min_quantity:
        violations.append("QUANTITY_BELOW_MINIMUM")
    if snapshot.max_quantity is not None and qty > snapshot.max_quantity:
        violations.append("QUANTITY_ABOVE_MAXIMUM")
    if intent.target.notional is not None:
        if snapshot.min_notional is not None and intent.target.notional < snapshot.min_notional:
            violations.append("NOTIONAL_BELOW_MINIMUM")
        if snapshot.max_notional is not None and intent.target.notional > snapshot.max_notional:
            violations.append("NOTIONAL_ABOVE_MAXIMUM")
    for name, price in (("LIMIT", intent.order.limit_price), ("STOP", intent.order.stop_price)):
        if price is None:
            continue
        if not _multiple(price, snapshot.tick_size):
            violations.append(f"{name}_PRICE_NOT_TICK_ALIGNED")
        if snapshot.min_price is not None and price < snapshot.min_price:
            violations.append(f"{name}_PRICE_BELOW_MINIMUM")
        if snapshot.max_price is not None and price > snapshot.max_price:
            violations.append(f"{name}_PRICE_ABOVE_MAXIMUM")
    if intent.asset_class == AssetClass.CRYPTO:
        product_type = intent.asset_extension.get("product_type")
        if not product_type:
            violations.append("CRYPTO_PRODUCT_TYPE_MISSING")
        elif product_type != snapshot.asset_extension.get("product_type"):
            violations.append("CRYPTO_PRODUCT_TYPE_MISMATCH")
    return ConstraintValidation(
        accepted=not violations,
        snapshot_id=snapshot.snapshot_id,
        snapshot_hash=snapshot.payload_hash,
        checked_at=checked_at,
        violations=tuple(violations),
    )


def crypto_perpetual_definition_to_constraint_snapshot(
    definition: PerpetualInstrumentDefinition,
    *,
    valid_until: datetime,
    source_ref: str | None = None,
    min_quantity: Decimal | None = None,
    max_quantity: Decimal | None = None,
    min_notional: Decimal | None = None,
    max_notional: Decimal | None = None,
    min_price: Decimal | None = None,
    max_price: Decimal | None = None,
    margin_mode: str | None = None,
) -> ConstraintSnapshot:
    definition.assert_usable_at(definition.available_at)
    source = source_ref or f"sha256:{definition.raw_sha256}"
    snapshot_id = "CNS-" + sha256(
        f"{definition.instrument_id}|{definition.venue}|{definition.available_at.isoformat()}|{definition.raw_sha256}".encode()
    ).hexdigest()[:48]
    return ConstraintSnapshot(
        snapshot_id=snapshot_id,
        asset_class=AssetClass.CRYPTO,
        instrument_id=definition.instrument_id,
        venue_id=definition.venue,
        venue_symbol=definition.venue_symbol,
        observed_at=definition.available_at,
        valid_until=valid_until,
        source_ref=source,
        source_version=definition.schema_version,
        status=definition.status,
        tick_size=definition.tick_size,
        lot_size=definition.lot_size,
        min_quantity=min_quantity,
        max_quantity=max_quantity,
        min_notional=min_notional,
        max_notional=max_notional,
        min_price=min_price,
        max_price=max_price,
        asset_extension={
            "product_type": "PERPETUAL",
            "contract_style": definition.contract_style,
            "contract_size": definition.contract_value,
            "margin_mode": margin_mode,
            "settle_asset": definition.settle_asset,
            "base_asset": definition.base_asset,
            "quote_asset": definition.quote_asset,
        },
    )


def _canonical_order(order: VenueOrder) -> CanonicalVenueOrder:
    return CanonicalVenueOrder(
        client_order_id=str(order.client_order_id),
        external_order_id=order.venue_order_id,
        venue_symbol=order.symbol,
        status=order.status.value,
        quantity=order.quantity,
        filled_quantity=order.filled_quantity,
    )


def normalize_adapter_exception(exc: Exception) -> AdapterError:
    """Normalize venue/client exceptions without guessing success or retry safety."""
    message = str(exc) or type(exc).__name__
    lowered = message.lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    external_code = getattr(exc, "code", None)
    if status == 429 or "rate limit" in lowered or "too many requests" in lowered:
        retry_after = getattr(exc, "retry_after", None)
        return AdapterError(
            AdapterErrorCategory.RATE_LIMIT, "RATE_LIMIT", message, retryable=True,
            retry_after_seconds=Decimal(str(retry_after)) if retry_after is not None else None,
            external_code=str(external_code) if external_code is not None else None,
        )
    if status in {401, 403} or "authentication" in lowered or "invalid api key" in lowered:
        category = AdapterErrorCategory.AUTHENTICATION if status == 401 or "authentication" in lowered or "api key" in lowered else AdapterErrorCategory.PERMISSION
        return AdapterError(category, "AUTH_OR_PERMISSION_FAILURE", message, retryable=False, external_code=str(external_code) if external_code is not None else None)
    if isinstance(exc, PermissionError) or "permission" in lowered or "not permitted" in lowered:
        return AdapterError(AdapterErrorCategory.PERMISSION, "PERMISSION_DENIED", message, retryable=False)
    if isinstance(exc, (ConnectionError, TimeoutError)) or "timeout" in lowered or "connection" in lowered:
        code = "TIMEOUT" if isinstance(exc, TimeoutError) or "timeout" in lowered else type(exc).__name__.upper()
        return AdapterError(AdapterErrorCategory.TRANSPORT, code, message, retryable=True)
    if isinstance(exc, KeyError):
        return AdapterError(AdapterErrorCategory.NOT_FOUND, "NOT_FOUND", message, retryable=False)
    if "reject" in lowered:
        return AdapterError(AdapterErrorCategory.VENUE_REJECT, "VENUE_REJECT", message, retryable=False, external_code=str(external_code) if external_code is not None else None)
    if isinstance(exc, ValueError):
        return AdapterError(AdapterErrorCategory.INVALID_REQUEST, type(exc).__name__.upper(), message, retryable=False)
    return AdapterError(AdapterErrorCategory.UNKNOWN, type(exc).__name__.upper(), message, retryable=False, external_code=str(external_code) if external_code is not None else None)


class CryptoBrokerAdapterBridge:
    """Canonical V1 wrapper around the current Crypto/legacy BrokerAdapter.

    Default construction is read-only/non-live. Trading methods are present to satisfy the
    canonical interface but cannot reach the wrapped adapter unless the capability evidence
    explicitly states TRADING_CAPABLE *and* production_submission_enabled. No such authority
    is manufactured here.
    """

    def __init__(
        self,
        adapter: BrokerAdapter,
        *,
        connection_capability: ConnectionCapability,
        constraint_snapshots: Mapping[str, ConstraintSnapshot],
        symbol_to_instrument_id: Mapping[str, str],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if Capability.WITHDRAWAL_CAPABLE in connection_capability.capabilities:
            raise ValueError("withdrawal capability must not be required by execution adapter")
        self.adapter = adapter
        self.connection_capability = connection_capability
        self._constraints = dict(constraint_snapshots)
        self._symbol_to_instrument = dict(symbol_to_instrument_id)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        for instrument_id, snap in self._constraints.items():
            if instrument_id != snap.instrument_id:
                raise ValueError("constraint snapshot map key must equal snapshot.instrument_id")
            if snap.venue_id != connection_capability.venue_id:
                raise ValueError("constraint venue must match connection capability venue")

    def _observed(self, at: datetime) -> datetime:
        _aware(at, "at")
        return at

    def _private_allowed(self) -> bool:
        return self.connection_capability.has(Capability.PRIVATE_READ_ONLY) or self.connection_capability.has(Capability.TRADING_CAPABLE)

    def _trading_allowed(self) -> bool:
        return self.connection_capability.has(Capability.TRADING_CAPABLE) and self.connection_capability.production_submission_enabled

    def get_instrument_constraints(self, instrument_id: str, *, at: datetime) -> AdapterResult[ConstraintSnapshot]:
        observed = self._observed(at)
        snap = self._constraints.get(instrument_id)
        if snap is None:
            return AdapterResult.failure(AdapterError(AdapterErrorCategory.NOT_FOUND, "CONSTRAINTS_UNKNOWN", f"no constraint snapshot for {instrument_id}"), observed)
        try:
            snap.assert_fresh(at)
        except ValueError as exc:
            return AdapterResult.failure(AdapterError(AdapterErrorCategory.STALE_STATE, "CONSTRAINTS_STALE", str(exc)), observed)
        return AdapterResult.success(snap, observed)

    def get_venue_state(self, *, at: datetime) -> AdapterResult[VenueState]:
        observed = self._observed(at)
        try:
            health: BrokerHealth = self.adapter.health()
        except Exception as exc:
            return AdapterResult.failure(normalize_adapter_exception(exc), observed)
        state = VenueOperationalState.TRADING if health.healthy else VenueOperationalState.DEGRADED
        return AdapterResult.success(VenueState(self.connection_capability.venue_id, state, observed, health.reason), observed)

    def get_market_session_state(self, instrument_id: str, *, at: datetime) -> AdapterResult[MarketSessionState]:
        observed = self._observed(at)
        constraints = self.get_instrument_constraints(instrument_id, at=at)
        if not constraints.ok:
            return AdapterResult.failure(constraints.error, observed)  # type: ignore[arg-type]
        state = SessionState.OPEN if constraints.value.status.upper() in {"TRADING", "OPEN", "ACTIVE", "LIVE"} else SessionState.HALTED  # type: ignore[union-attr]
        return AdapterResult.success(MarketSessionState(self.connection_capability.venue_id, instrument_id, state, observed, "CRYPTO_24X7"), observed)

    def get_account_state(self, *, at: datetime) -> AdapterResult[AccountStateSnapshot]:
        observed = self._observed(at)
        if not self._private_allowed():
            return AdapterResult.failure(AdapterError(AdapterErrorCategory.PERMISSION, "PRIVATE_READ_CAPABILITY_REQUIRED", "private read capability is not verified"), observed)
        try:
            snap = self.adapter.account_snapshot()
        except Exception as exc:
            return AdapterResult.failure(normalize_adapter_exception(exc), observed)
        positions = tuple(
            PositionSnapshot(self._symbol_to_instrument.get(symbol, symbol), quantity)
            for symbol, quantity in sorted(snap.positions.items())
        )
        open_orders = tuple(_canonical_order(order) for order in snap.open_orders)
        return AdapterResult.success(
            AccountStateSnapshot(self.connection_capability.venue_id, self.connection_capability.connection_id, snap.observed_at, positions, open_orders),
            snap.observed_at,
        )

    def list_open_orders(self, *, at: datetime) -> AdapterResult[tuple[CanonicalVenueOrder, ...]]:
        account = self.get_account_state(at=at)
        if not account.ok:
            return AdapterResult.failure(account.error, account.observed_at)  # type: ignore[arg-type]
        return AdapterResult.success(account.value.open_orders, account.observed_at)  # type: ignore[union-attr]

    def list_recent_fills(self, *, at: datetime) -> AdapterResult[tuple[CanonicalVenueFill, ...]]:
        observed = self._observed(at)
        return AdapterResult.failure(
            AdapterError(AdapterErrorCategory.UNSUPPORTED, "RECENT_FILLS_UNAVAILABLE", "legacy Crypto BrokerAdapter exposes no private recent-fill method"),
            observed,
        )

    def list_recent_fees(self, *, at: datetime) -> AdapterResult[tuple[CanonicalVenueFee, ...]]:
        observed = self._observed(at)
        return AdapterResult.failure(
            AdapterError(AdapterErrorCategory.UNSUPPORTED, "RECENT_FEES_UNAVAILABLE", "legacy Crypto BrokerAdapter exposes no private fee method"),
            observed,
        )

    def get_balances(self, *, at: datetime) -> AdapterResult[tuple[BalanceSnapshot, ...]]:
        observed = self._observed(at)
        return AdapterResult.failure(
            AdapterError(AdapterErrorCategory.UNSUPPORTED, "BALANCES_UNAVAILABLE", "legacy Crypto BrokerAdapter account snapshot exposes no balances"),
            observed,
        )

    def get_positions(self, *, at: datetime) -> AdapterResult[tuple[PositionSnapshot, ...]]:
        account = self.get_account_state(at=at)
        if not account.ok:
            return AdapterResult.failure(account.error, account.observed_at)  # type: ignore[arg-type]
        return AdapterResult.success(account.value.positions, account.observed_at)  # type: ignore[union-attr]

    def get_order(self, client_order_id: str, *, at: datetime) -> AdapterResult[CanonicalVenueOrder]:
        orders = self.list_open_orders(at=at)
        if not orders.ok:
            return AdapterResult.failure(orders.error, orders.observed_at)  # type: ignore[arg-type]
        for order in orders.value or ():
            if order.client_order_id == client_order_id:
                return AdapterResult.success(order, orders.observed_at)
        return AdapterResult.failure(AdapterError(AdapterErrorCategory.NOT_FOUND, "ORDER_NOT_FOUND", f"open order {client_order_id} not found"), orders.observed_at)

    def submit_order(self, intent: OrderIntent, *, action_id: str, at: datetime) -> AdapterResult[CanonicalVenueOrder]:
        observed = self._observed(at)
        if not self._trading_allowed():
            return AdapterResult.failure(AdapterError(AdapterErrorCategory.TRADING_DISABLED, "TRADING_CAPABILITY_DISABLED", "production trading is not enabled by connection capability"), observed)
        constraint_result = self.get_instrument_constraints(intent.instrument.instrument_id, at=at)
        if not constraint_result.ok:
            return AdapterResult.failure(constraint_result.error, observed)  # type: ignore[arg-type]
        validation = validate_order_intent_against_constraints(intent, constraint_result.value, checked_at=at)  # type: ignore[arg-type]
        if not validation.accepted:
            return AdapterResult.failure(AdapterError(AdapterErrorCategory.CONSTRAINT_VIOLATION, "ORDER_CONSTRAINT_VIOLATION", ",".join(validation.violations)), observed)
        if intent.order.order_type not in {OrderTypeV1.MARKET, OrderTypeV1.LIMIT}:
            return AdapterResult.failure(AdapterError(AdapterErrorCategory.UNSUPPORTED, "ORDER_TYPE_UNSUPPORTED", intent.order.order_type.value), observed)
        try:
            strategy_id = UUID(intent.strategy.strategy_id)
        except ValueError:
            strategy_id = uuid5(_ADAPTER_NAMESPACE, intent.strategy.strategy_id)
        legacy = OrderRequest(
            strategy_id=strategy_id,
            symbol=intent.instrument.venue_symbol or intent.instrument.instrument_id,
            side=Side(intent.target.side),
            quantity=intent.target.quantity,
            order_type=OrderType(intent.order.order_type.value),
            decision_time=intent.decision_timestamp,
            reference_price=(intent.target.notional / intent.target.quantity) if intent.target.notional is not None else (intent.order.limit_price or Decimal("1")),
            limit_price=intent.order.limit_price,
            reduce_only=intent.order.reduce_only,
            order_id=uuid5(_ADAPTER_NAMESPACE, action_id),
        )
        try:
            return AdapterResult.success(_canonical_order(self.adapter.submit_order(legacy)), observed)
        except Exception as exc:
            return AdapterResult.failure(normalize_adapter_exception(exc), observed)

    def cancel_order(self, client_order_id: str, *, action_id: str, at: datetime) -> AdapterResult[bool]:
        observed = self._observed(at)
        if not self._trading_allowed():
            return AdapterResult.failure(AdapterError(AdapterErrorCategory.TRADING_DISABLED, "TRADING_CAPABILITY_DISABLED", "production trading is not enabled by connection capability"), observed)
        try:
            self.adapter.cancel_order(UUID(client_order_id))
            return AdapterResult.success(True, observed)
        except Exception as exc:
            return AdapterResult.failure(normalize_adapter_exception(exc), observed)


__all__ = [
    "AccountStateSnapshot", "AdapterError", "AdapterErrorCategory", "AdapterResult",
    "CanonicalVenueAdapter", "CanonicalVenueOrder", "ConstraintSnapshot", "ConstraintValidation",
    "CryptoBrokerAdapterBridge", "MarketSessionState", "PositionSnapshot", "SessionState",
    "VenueOperationalState", "VenueState", "crypto_perpetual_definition_to_constraint_snapshot",
    "validate_order_intent_against_constraints", "normalize_adapter_exception",
]
