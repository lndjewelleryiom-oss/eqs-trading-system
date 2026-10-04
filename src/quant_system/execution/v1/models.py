from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Iterable, Mapping


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _text(value: str, name: str, *, minimum: int = 1, maximum: int = 192) -> None:
    if not isinstance(value, str) or len(value) < minimum or len(value) > maximum:
        raise ValueError(f"{name} length must be in [{minimum},{maximum}]")


def _nonnegative(value: Decimal, name: str) -> None:
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _positive(value: Decimal, name: str) -> None:
    if value <= 0:
        raise ValueError(f"{name} must be positive")


def _sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _wire(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return {f.name: _wire(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, tuple):
        return [_wire(item) for item in value]
    if isinstance(value, list):
        return [_wire(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _wire(v) for k, v in value.items()}
    return value


def _canonical_hash(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(_wire(dict(payload)), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(raw).hexdigest()


class AssetClass(StrEnum):
    CRYPTO = "CRYPTO"
    EQUITY = "EQUITY"
    ETF = "ETF"
    FX = "FX"
    FUTURE = "FUTURE"
    COMMODITY = "COMMODITY"
    RATE = "RATE"
    OPTION = "OPTION"


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    SHADOW = "SHADOW"
    LIVE = "LIVE"


class Capability(StrEnum):
    PUBLIC_ONLY = "PUBLIC_ONLY"
    PRIVATE_READ_ONLY = "PRIVATE_READ_ONLY"
    TRADING_CAPABLE = "TRADING_CAPABLE"
    WITHDRAWAL_CAPABLE = "WITHDRAWAL_CAPABLE"


class InterlockState(StrEnum):
    DISABLED = "DISABLED"
    PAPER_ONLY = "PAPER_ONLY"
    SHADOW_ZERO_SUBMIT = "SHADOW_ZERO_SUBMIT"
    LIVE_AUTHORISED = "LIVE_AUTHORISED"


class InterlockIssuer(StrEnum):
    EQS_00 = "EQS-00"
    AUTHORISED_OWNER_CONTROL = "AUTHORISED_OWNER_CONTROL"


class OrderTypeV1(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    PEGGED = "PEGGED"
    AUCTION = "AUCTION"
    COMPLEX = "COMPLEX"


class TimeInForce(StrEnum):
    DAY = "DAY"
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    GTD = "GTD"
    OPG = "OPG"
    CLS = "CLS"


class ExecutionEventType(StrEnum):
    SUBMISSION_ATTEMPT = "SUBMISSION_ATTEMPT"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCEL_ACKNOWLEDGED = "CANCEL_ACKNOWLEDGED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    RECONCILIATION_OBSERVATION = "RECONCILIATION_OBSERVATION"
    UNKNOWN_SUBMISSION_OUTCOME = "UNKNOWN_SUBMISSION_OUTCOME"


class ReconciliationStatus(StrEnum):
    MATCHED = "MATCHED"
    DEGRADED = "DEGRADED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    BLOCKED_UNKNOWN = "BLOCKED_UNKNOWN"


class UnresolvedKind(StrEnum):
    ORDER = "ORDER"
    FILL = "FILL"
    POSITION = "POSITION"
    FEE = "FEE"
    BALANCE = "BALANCE"
    SUBMISSION_OUTCOME = "SUBMISSION_OUTCOME"
    CONSTRAINT = "CONSTRAINT"
    VENUE_STATE = "VENUE_STATE"


class SettlementEvent(StrEnum):
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    RECONCILIATION_ADJUSTMENT = "RECONCILIATION_ADJUSTMENT"


@dataclass(frozen=True, slots=True)
class StrategyRef:
    strategy_id: str
    strategy_version: str
    campaign_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.strategy_id, "strategy_id", maximum=128)
        _text(self.strategy_version, "strategy_version", maximum=64)
        if self.campaign_id is not None:
            _text(self.campaign_id, "campaign_id", maximum=128)


@dataclass(frozen=True, slots=True)
class ReservationRef:
    reservation_id: str
    risk_authorisation_id: str
    authorised_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        _text(self.reservation_id, "reservation_id", minimum=8, maximum=128)
        _text(self.risk_authorisation_id, "risk_authorisation_id", minimum=8, maximum=128)
        _aware(self.authorised_at, "authorised_at")
        _aware(self.expires_at, "expires_at")
        if self.expires_at <= self.authorised_at:
            raise ValueError("reservation expires_at must be after authorised_at")


@dataclass(frozen=True, slots=True)
class InstrumentRef:
    instrument_id: str
    currency: str
    venue_symbol: str | None = None
    underlying_id: str | None = None
    contract_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.instrument_id, "instrument_id")
        if not (3 <= len(self.currency) <= 12 and self.currency.isalnum() and self.currency.upper() == self.currency):
            raise ValueError("currency must be 3-12 uppercase alphanumeric characters")
        for name, value, maximum in (
            ("venue_symbol", self.venue_symbol, 128),
            ("underlying_id", self.underlying_id, 192),
            ("contract_id", self.contract_id, 192),
        ):
            if value is not None:
                _text(value, name, maximum=maximum)


@dataclass(frozen=True, slots=True)
class Target:
    side: str
    quantity: Decimal
    notional: Decimal | None = None

    def __post_init__(self) -> None:
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        _positive(self.quantity, "quantity")
        if self.notional is not None:
            _nonnegative(self.notional, "notional")


@dataclass(frozen=True, slots=True)
class OrderSpec:
    order_type: OrderTypeV1
    time_in_force: TimeInForce
    limit_price: Decimal | None = None
    stop_price: Decimal | None = None
    expire_at: datetime | None = None
    post_only: bool = False
    reduce_only: bool = False

    def __post_init__(self) -> None:
        if self.order_type in {OrderTypeV1.LIMIT, OrderTypeV1.STOP_LIMIT, OrderTypeV1.COMPLEX} and self.limit_price is None:
            raise ValueError(f"{self.order_type.value} requires limit_price")
        if self.order_type in {OrderTypeV1.STOP, OrderTypeV1.STOP_LIMIT} and self.stop_price is None:
            raise ValueError(f"{self.order_type.value} requires stop_price")
        if self.time_in_force == TimeInForce.GTD and self.expire_at is None:
            raise ValueError("GTD requires expire_at")
        for name, value in (("limit_price", self.limit_price), ("stop_price", self.stop_price)):
            if value is not None:
                _positive(value, name)
        if self.expire_at is not None:
            _aware(self.expire_at, "expire_at")


@dataclass(frozen=True, slots=True)
class Route:
    venue_id: str
    connection_id: str
    required_capability: Capability

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id", maximum=64)
        _text(self.connection_id, "connection_id", maximum=128)
        if self.required_capability == Capability.WITHDRAWAL_CAPABLE:
            raise ValueError("withdrawal capability is never an execution requirement")


@dataclass(frozen=True, slots=True)
class AuthorisationRef:
    programme_state_ref: str
    interlock_ref: str

    def __post_init__(self) -> None:
        _text(self.programme_state_ref, "programme_state_ref", minimum=8)
        _text(self.interlock_ref, "interlock_ref", minimum=8)


@dataclass(frozen=True, slots=True)
class OrderLeg:
    leg_id: str
    instrument_id: str
    side: str
    ratio: int
    quantity: Decimal

    def __post_init__(self) -> None:
        _text(self.leg_id, "leg_id", maximum=64)
        _text(self.instrument_id, "instrument_id")
        if self.side not in {"BUY", "SELL"}:
            raise ValueError("leg side must be BUY or SELL")
        if not 1 <= self.ratio <= 1000:
            raise ValueError("leg ratio must be in [1,1000]")
        _positive(self.quantity, "leg quantity")


@dataclass(frozen=True, slots=True)
class OrderIntent:
    execution_intent_id: str
    request_id: str
    portfolio_decision_id: str
    strategy: StrategyRef
    portfolio_id: str
    reservation: ReservationRef
    asset_class: AssetClass
    instrument: InstrumentRef
    target: Target
    order: OrderSpec
    route: Route
    mode: ExecutionMode
    decision_timestamp: datetime
    authorisation: AuthorisationRef
    market_state_ref: str
    reference_data_ref: str
    parent_execution_intent_id: str | None = None
    strategy_order_group_id: str | None = None
    legs: tuple[OrderLeg, ...] | None = None
    asset_extension: Mapping[str, Any] = field(default_factory=dict)
    schema_version: str = field(default="EQS-EXEC-ORDER-INTENT-v1.0", init=False)

    def __post_init__(self) -> None:
        _text(self.execution_intent_id, "execution_intent_id", minimum=16, maximum=128)
        _text(self.request_id, "request_id", minimum=8, maximum=128)
        _text(self.portfolio_decision_id, "portfolio_decision_id", minimum=8, maximum=128)
        _text(self.portfolio_id, "portfolio_id", maximum=128)
        _aware(self.decision_timestamp, "decision_timestamp")
        _text(self.market_state_ref, "market_state_ref", minimum=8)
        _text(self.reference_data_ref, "reference_data_ref", minimum=8)
        if self.decision_timestamp > self.reservation.expires_at:
            raise ValueError("decision_timestamp is after reservation expiry")
        if self.mode == ExecutionMode.LIVE and self.route.required_capability != Capability.TRADING_CAPABLE:
            raise ValueError("LIVE intent requires TRADING_CAPABLE route")
        if self.order.time_in_force == TimeInForce.GTD and self.order.expire_at is not None and self.order.expire_at <= self.decision_timestamp:
            raise ValueError("GTD expire_at must be after decision_timestamp")
        if self.legs is not None and not 2 <= len(self.legs) <= 32:
            raise ValueError("legs must contain 2-32 entries")
        if self.asset_class == AssetClass.OPTION and self.order.order_type == OrderTypeV1.COMPLEX:
            if not self.legs or self.strategy_order_group_id is None:
                raise ValueError("complex option intent requires legs and strategy_order_group_id")
        if self.parent_execution_intent_id is not None:
            _text(self.parent_execution_intent_id, "parent_execution_intent_id", maximum=128)
        if self.strategy_order_group_id is not None:
            _text(self.strategy_order_group_id, "strategy_order_group_id", maximum=128)
        object.__setattr__(self, "asset_extension", dict(self.asset_extension))

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)


@dataclass(frozen=True, slots=True)
class SubmissionInterlock:
    interlock_id: str
    revision: int
    state: InterlockState
    issued_by: InterlockIssuer
    valid_from: datetime
    valid_until: datetime
    programme_state_ref: str
    seal_hash: str
    allowed_strategy_ids: tuple[str, ...] = ()
    allowed_venue_ids: tuple[str, ...] = ()
    allowed_instrument_ids: tuple[str, ...] = ()
    schema_version: str = field(default="EQS-EXEC-INTERLOCK-v1.0", init=False)

    def __post_init__(self) -> None:
        _text(self.interlock_id, "interlock_id", minimum=8, maximum=128)
        if self.revision < 1:
            raise ValueError("revision must be >= 1")
        _aware(self.valid_from, "valid_from")
        _aware(self.valid_until, "valid_until")
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until must be after valid_from")
        _text(self.programme_state_ref, "programme_state_ref", minimum=8)
        _sha256(self.seal_hash, "seal_hash")
        for values in (self.allowed_strategy_ids, self.allowed_venue_ids, self.allowed_instrument_ids):
            if len(values) != len(set(values)):
                raise ValueError("interlock scope lists must be unique")

    def assert_allows(self, intent: OrderIntent, *, at: datetime) -> None:
        _aware(at, "at")
        if not self.valid_from <= at <= self.valid_until:
            raise PermissionError("submission interlock is outside its validity window")
        expected = {
            ExecutionMode.PAPER: {InterlockState.PAPER_ONLY, InterlockState.SHADOW_ZERO_SUBMIT, InterlockState.LIVE_AUTHORISED},
            ExecutionMode.SHADOW: {InterlockState.SHADOW_ZERO_SUBMIT, InterlockState.LIVE_AUTHORISED},
            ExecutionMode.LIVE: {InterlockState.LIVE_AUTHORISED},
        }[intent.mode]
        if self.state not in expected:
            raise PermissionError(f"interlock {self.state.value} does not allow {intent.mode.value}")
        if self.allowed_strategy_ids and intent.strategy.strategy_id not in self.allowed_strategy_ids:
            raise PermissionError("strategy is outside interlock scope")
        if self.allowed_venue_ids and intent.route.venue_id not in self.allowed_venue_ids:
            raise PermissionError("venue is outside interlock scope")
        if self.allowed_instrument_ids and intent.instrument.instrument_id not in self.allowed_instrument_ids:
            raise PermissionError("instrument is outside interlock scope")

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)


@dataclass(frozen=True, slots=True)
class ConnectionCapability:
    connection_id: str
    venue_id: str
    capabilities: tuple[Capability, ...]
    verified_at: datetime
    verification_ref: str
    production_submission_enabled: bool
    schema_version: str = field(default="EQS-EXEC-CAPABILITY-v1.0", init=False)

    def __post_init__(self) -> None:
        _text(self.connection_id, "connection_id", maximum=128)
        _text(self.venue_id, "venue_id", maximum=64)
        if not self.capabilities or len(self.capabilities) != len(set(self.capabilities)):
            raise ValueError("capabilities must be a non-empty unique tuple")
        _aware(self.verified_at, "verified_at")
        _text(self.verification_ref, "verification_ref", minimum=8)
        if self.production_submission_enabled and Capability.TRADING_CAPABLE not in self.capabilities:
            raise ValueError("production submission cannot be enabled without TRADING_CAPABLE evidence")

    def has(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)


@dataclass(frozen=True, slots=True)
class FillDetail:
    fill_id: str
    quantity: Decimal
    price: Decimal
    liquidity_role: str = "UNKNOWN"

    def __post_init__(self) -> None:
        _text(self.fill_id, "fill_id")
        _positive(self.quantity, "fill quantity")
        _positive(self.price, "fill price")
        if self.liquidity_role not in {"MAKER", "TAKER", "UNKNOWN"}:
            raise ValueError("invalid liquidity_role")


@dataclass(frozen=True, slots=True)
class FeeDetail:
    fee_type: str
    amount: Decimal
    currency: str

    def __post_init__(self) -> None:
        if self.fee_type not in {"COMMISSION", "EXCHANGE", "BROKER", "REGULATORY", "REBATE", "FUNDING", "FINANCING", "TAX", "OTHER"}:
            raise ValueError("invalid fee_type")
        if not (3 <= len(self.currency) <= 12 and self.currency.isalnum() and self.currency.upper() == self.currency):
            raise ValueError("invalid fee currency")


@dataclass(frozen=True, slots=True)
class ExecutionEvent:
    event_id: str
    execution_id: str
    client_order_id: str
    venue_id: str
    event_type: ExecutionEventType
    receive_timestamp: datetime
    payload_hash: str
    external_order_id: str | None = None
    venue_timestamp: datetime | None = None
    sequence: int | None = None
    fill: FillDetail | None = None
    fees: tuple[FeeDetail, ...] = ()
    reject_code: str | None = None
    reject_reason: str | None = None
    raw_receipt_hash: str | None = None
    schema_version: str = field(default="EQS-EXEC-TRUTH-EVENT-v1.0", init=False)

    def __post_init__(self) -> None:
        for name, value in (("event_id", self.event_id), ("execution_id", self.execution_id), ("client_order_id", self.client_order_id)):
            _text(value, name, minimum=8, maximum=128)
        _text(self.venue_id, "venue_id", maximum=64)
        _aware(self.receive_timestamp, "receive_timestamp")
        if self.venue_timestamp is not None:
            _aware(self.venue_timestamp, "venue_timestamp")
        if self.sequence is not None and self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if self.event_type in {ExecutionEventType.PARTIAL_FILL, ExecutionEventType.FILL} and self.fill is None:
            raise ValueError("fill event requires fill detail")
        if self.event_type == ExecutionEventType.REJECTED and (not self.reject_code or not self.reject_reason):
            raise ValueError("REJECTED requires reject_code and reject_reason")
        if self.raw_receipt_hash is not None:
            _sha256(self.raw_receipt_hash, "raw_receipt_hash")
        _sha256(self.payload_hash, "payload_hash")

    @classmethod
    def build(cls, **kwargs: Any) -> "ExecutionEvent":
        provisional = dict(kwargs)
        provisional["schema_version"] = "EQS-EXEC-TRUTH-EVENT-v1.0"
        provisional.pop("payload_hash", None)
        payload_hash = _canonical_hash(provisional)
        return cls(payload_hash=payload_hash, **kwargs)

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)


@dataclass(frozen=True, slots=True)
class ReconciliationCounts:
    local_open_orders: int
    external_open_orders: int
    fills_seen: int
    position_mismatches: int
    unknown_orders: int

    def __post_init__(self) -> None:
        for f in fields(self):
            if getattr(self, f.name) < 0:
                raise ValueError(f"{f.name} must be non-negative")


@dataclass(frozen=True, slots=True)
class UnresolvedItem:
    kind: UnresolvedKind
    reference: str
    safety_critical: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        _text(self.reference, "unresolved reference")
        if self.reason is not None:
            _text(self.reason, "unresolved reason", maximum=512)


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    reconciliation_id: str
    venue_id: str
    connection_id: str
    started_at: datetime
    completed_at: datetime
    status: ReconciliationStatus
    counts: ReconciliationCounts
    unresolved: tuple[UnresolvedItem, ...]
    evidence_refs: tuple[str, ...] = ()
    schema_version: str = field(default="EQS-EXEC-RECON-v1.0", init=False)

    def __post_init__(self) -> None:
        _text(self.reconciliation_id, "reconciliation_id", minimum=8, maximum=128)
        _text(self.venue_id, "venue_id", maximum=64)
        _text(self.connection_id, "connection_id", maximum=128)
        _aware(self.started_at, "started_at")
        _aware(self.completed_at, "completed_at")
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot be before started_at")
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("evidence_refs must be unique")
        if self.status == ReconciliationStatus.MATCHED and self.unresolved:
            raise ValueError("MATCHED reconciliation cannot contain unresolved items")

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)


@dataclass(frozen=True, slots=True)
class ReservationSettlement:
    settlement_id: str
    reservation_id: str
    execution_id: str
    event: SettlementEvent
    filled_quantity: Decimal
    remaining_quantity: Decimal
    actual_notional: Decimal
    actual_fees: Decimal
    timestamp: datetime
    execution_truth_ref: str | None = None
    schema_version: str = field(default="EQS-EXEC-RESERVATION-SETTLEMENT-v1.0", init=False)

    def __post_init__(self) -> None:
        for name, value in (("settlement_id", self.settlement_id), ("reservation_id", self.reservation_id), ("execution_id", self.execution_id)):
            _text(value, name, minimum=8, maximum=128)
        for name, value in (("filled_quantity", self.filled_quantity), ("remaining_quantity", self.remaining_quantity), ("actual_notional", self.actual_notional)):
            _nonnegative(value, name)
        _aware(self.timestamp, "timestamp")
        if self.execution_truth_ref is not None:
            _text(self.execution_truth_ref, "execution_truth_ref", maximum=192)

    def to_payload(self) -> dict[str, Any]:
        return _wire(self)
