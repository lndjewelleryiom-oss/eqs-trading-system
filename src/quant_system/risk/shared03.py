from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from hashlib import sha256
import json
from typing import Any, Mapping, Sequence
from uuid import UUID

from quant_system.execution.models import OrderRequest
from quant_system.risk.contracts import ReservationState, RiskReservation

SCHEMA_VERSION = "EQS-SHARED-03-v1.0"
UPSTREAM_V1_BUNDLE_SHA256 = "ae84d3fd221857ab3bf2b3dc0f95de26bc43309f1d55b2ca1a4b03fcee39f0c6"
UPSTREAM_V1_MANIFEST_SHA256 = "8dbe58730839a3d69ba7f00af6da13870dcee104fea11b52b16aa80d634afbe2"

def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")

def _canonical(value: Any) -> Any:
    if isinstance(value, Decimal): return str(value)
    if isinstance(value, datetime): return value.isoformat()
    if isinstance(value, UUID): return str(value)
    if isinstance(value, StrEnum): return value.value
    if isinstance(value, Mapping):
        return {str(k): _canonical(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (tuple, list)): return [_canonical(v) for v in value]
    return value

def fingerprint(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return sha256(raw).hexdigest()

class Shared03AssetClass(StrEnum):
    CRYPTO="CRYPTO"; EQUITY="EQUITY"; ETF="ETF"; FX="FX"
    FUTURE="FUTURE"; COMMODITY="COMMODITY"; RATE="RATE"; OPTION="OPTION"

class Shared03ReservationState(StrEnum):
    REQUESTED="requested"; APPROVED="approved"; REJECTED="rejected"; COMMITTED="committed"
    PARTIALLY_CONSUMED="partially_consumed"; RELEASED="released"; EXPIRED="expired"; RECONCILED="reconciled"

class Shared03GateDisposition(StrEnum):
    ALLOW="ALLOW"; BLOCK="BLOCK"; HALT="HALT"

@dataclass(frozen=True, slots=True)
class Shared03CapitalState:
    account_id: str
    portfolio_id: str
    cash: Decimal | None
    equity: Decimal | None
    available_capital: Decimal | None
    reserved_capital: Decimal | None
    deployed_capital: Decimal | None
    collateral: Decimal | None
    margin: Decimal | None
    realised_pnl: Decimal | None
    unrealised_pnl: Decimal | None
    as_of: datetime
    valid_until: datetime | None
    account_state_known: bool = True
    reconciliation_clean: bool = True
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _aware(self.as_of, "as_of")
        if self.valid_until is not None: _aware(self.valid_until, "valid_until")

    def to_payload(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}

    @property
    def state_fingerprint(self) -> str:
        return fingerprint(self.to_payload())

    def validate(self, now: datetime) -> tuple[str, ...]:
        _aware(now, "now")
        reasons: list[str] = []
        critical = (self.cash, self.equity, self.available_capital, self.reserved_capital,
                    self.deployed_capital, self.collateral, self.margin, self.realised_pnl, self.unrealised_pnl)
        if any(value is None for value in critical): reasons.append("CAPITAL_STATE_UNKNOWN")
        if not self.account_state_known: reasons.append("ACCOUNT_STATE_UNKNOWN")
        if not self.reconciliation_clean: reasons.append("CAPITAL_RECONCILIATION_UNCLEAN")
        if self.as_of > now: reasons.append("CAPITAL_STATE_FROM_FUTURE")
        if self.valid_until is None or now > self.valid_until: reasons.append("CAPITAL_STATE_STALE_OR_UNBOUNDED")
        for name in ("available_capital","reserved_capital","deployed_capital","collateral","margin"):
            value = getattr(self, name)
            if value is not None and value < 0: reasons.append(f"{name.upper()}_NEGATIVE")
        return tuple(sorted(set(reasons)))

@dataclass(frozen=True, slots=True)
class Shared03ReservationView:
    reservation_id: str
    intent_id: str
    portfolio_id: str
    strategy_id: str
    instrument: str
    venue: str
    state: Shared03ReservationState
    reserved_notional: Decimal
    consumed_notional: Decimal
    expires_at: datetime | None
    execution_state_known: bool = True
    reconciliation_clean: bool = True
    source_ref: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.expires_at is not None: _aware(self.expires_at, "expires_at")
        if self.reserved_notional < 0 or self.consumed_notional < 0:
            raise ValueError("reservation notionals must be non-negative")
        if self.consumed_notional > self.reserved_notional:
            raise ValueError("consumed_notional exceeds reserved_notional")

    @property
    def remaining_notional(self) -> Decimal:
        return self.reserved_notional - self.consumed_notional

    def to_payload(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__slots__}

    @property
    def reservation_fingerprint(self) -> str:
        return fingerprint(self.to_payload())

    def validate_for_new_risk(self, order: OrderRequest, now: datetime) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.intent_id != str(order.order_id): reasons.append("RESERVATION_INTENT_MISMATCH")
        if self.strategy_id != str(order.strategy_id): reasons.append("RESERVATION_STRATEGY_MISMATCH")
        if self.instrument != order.symbol: reasons.append("RESERVATION_INSTRUMENT_MISMATCH")
        if not self.execution_state_known: reasons.append("RESERVATION_EXECUTION_STATE_UNKNOWN")
        if not self.reconciliation_clean: reasons.append("RESERVATION_RECONCILIATION_UNCLEAN")
        if self.expires_at is not None and now >= self.expires_at: reasons.append("RESERVATION_EXPIRED")
        if self.state not in {Shared03ReservationState.APPROVED, Shared03ReservationState.COMMITTED, Shared03ReservationState.PARTIALLY_CONSUMED}:
            reasons.append("RESERVATION_NOT_ACTIVE")
        if self.remaining_notional < order.notional: reasons.append("RESERVATION_CAPACITY_INSUFFICIENT")
        return tuple(sorted(set(reasons)))

    @classmethod
    def from_risk_reservation(cls, reservation: RiskReservation, *, execution_state_known: bool=True,
                              reconciliation_clean: bool=True, source_ref: str | None=None) -> "Shared03ReservationView":
        state_map = {
            ReservationState.RESERVED: Shared03ReservationState.COMMITTED,
            ReservationState.PARTIALLY_CONSUMED: Shared03ReservationState.PARTIALLY_CONSUMED,
            ReservationState.CONSUMED: Shared03ReservationState.RECONCILED,
            ReservationState.RELEASED: Shared03ReservationState.RELEASED,
            ReservationState.UNKNOWN_EXECUTION: Shared03ReservationState.COMMITTED,
        }
        known = execution_state_known and reservation.state != ReservationState.UNKNOWN_EXECUTION
        return cls(str(reservation.reservation_id), str(reservation.intent_id), reservation.portfolio_id,
                   str(reservation.strategy_id), reservation.instrument, reservation.venue,
                   state_map[reservation.state], reservation.reserved_notional, reservation.consumed_notional,
                   reservation.expires_at, known, reconciliation_clean, source_ref)

@dataclass(frozen=True, slots=True)
class Shared03ExposureView:
    exposure_id: str
    asset_class: Shared03AssetClass
    strategy_id: str
    instrument: str
    venue: str
    signed_notional: Decimal | None
    currency: str | None
    attributes: Mapping[str, Any] = field(default_factory=dict)
    measurement_status: str = "KNOWN"
    schema_version: str = SCHEMA_VERSION

    def to_payload(self) -> dict[str, Any]:
        return {"exposure_id":self.exposure_id,"asset_class":self.asset_class,"strategy_id":self.strategy_id,
                "instrument":self.instrument,"venue":self.venue,"signed_notional":self.signed_notional,
                "currency":self.currency,"attributes":dict(self.attributes),
                "measurement_status":self.measurement_status,"schema_version":self.schema_version}

    @property
    def exposure_fingerprint(self) -> str:
        return fingerprint(self.to_payload())

    def validate(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.measurement_status != "KNOWN" or self.signed_notional is None:
            return ("EXPOSURE_MEASUREMENT_UNKNOWN",)
        attrs = self.attributes
        if self.asset_class in {Shared03AssetClass.EQUITY, Shared03AssetClass.ETF}:
            for key in ("long_short_notional","beta_exposure","liquidity_state"):
                if key not in attrs: reasons.append(f"EQUITY_{key.upper()}_MISSING")
            if self.signed_notional < 0 and attrs.get("borrow_state") not in {"AVAILABLE","LOCATED","KNOWN"}:
                reasons.append("EQUITY_BORROW_STATE_UNKNOWN")
        elif self.asset_class == Shared03AssetClass.FX:
            legs = attrs.get("currency_legs")
            if not isinstance(legs, Sequence) or isinstance(legs,(str,bytes)) or len(legs)<2:
                reasons.append("FX_CURRENCY_DECOMPOSITION_MISSING")
            else:
                for leg in legs:
                    if not isinstance(leg, Mapping) or not leg.get("currency") or leg.get("amount") is None:
                        reasons.append("FX_CURRENCY_LEG_INVALID"); break
                    if leg.get("measurement_status","KNOWN") != "KNOWN":
                        reasons.append("FX_CURRENCY_LEG_UNKNOWN"); break
        elif self.asset_class == Shared03AssetClass.FUTURE:
            for key in ("contract_multiplier","contract_count","underlying_notional","margin_requirement","expiry"):
                if key not in attrs: reasons.append(f"FUTURES_{key.upper()}_MISSING")
            if attrs.get("economic_exposure_basis") == "MARGIN":
                reasons.append("FUTURES_MARGIN_NOT_ECONOMIC_EXPOSURE")
        elif self.asset_class == Shared03AssetClass.COMMODITY:
            form = attrs.get("instrument_form")
            if form == "SPOT":
                for key in ("unit","quote_currency","underlying_notional","liquidity_state"):
                    if key not in attrs: reasons.append(f"COMMODITY_SPOT_{key.upper()}_MISSING")
                if attrs.get("quote_currency") not in {None, self.currency}:
                    reasons.append("COMMODITY_SPOT_QUOTE_CURRENCY_MISMATCH")
            elif form == "FUTURE":
                for key in ("contract_multiplier","contract_count","underlying_notional","margin_requirement","expiry"):
                    if key not in attrs: reasons.append(f"FUTURES_{key.upper()}_MISSING")
                if attrs.get("economic_exposure_basis") == "MARGIN":
                    reasons.append("FUTURES_MARGIN_NOT_ECONOMIC_EXPOSURE")
            else:
                reasons.append("COMMODITY_INSTRUMENT_FORM_MISSING_OR_UNSUPPORTED")
        elif self.asset_class == Shared03AssetClass.RATE:
            for key in ("duration","dv01","pv01","key_rate_dv01","convexity"):
                if key not in attrs: reasons.append(f"RATES_{key.upper()}_MISSING")
        elif self.asset_class == Shared03AssetClass.OPTION:
            for key in ("premium","delta","gamma","vega","theta","rho","expiry","strike","assignment_risk","undefined_risk_state","stress_loss_state"):
                if key not in attrs: reasons.append(f"OPTIONS_{key.upper()}_MISSING")
            if attrs.get("undefined_risk_state") in {None,"UNKNOWN"}: reasons.append("OPTIONS_UNDEFINED_RISK_UNKNOWN")
            if attrs.get("stress_loss_state") in {None,"UNKNOWN"}: reasons.append("OPTIONS_STRESS_STATE_UNKNOWN")
        return tuple(sorted(set(reasons)))

@dataclass(frozen=True, slots=True)
class Shared03Context:
    execution_mode: str
    capital_state: Shared03CapitalState
    reservation: Shared03ReservationView | None
    exposures: tuple[Shared03ExposureView, ...]
    market_data_fresh: bool
    leverage_known: bool
    reconciliation_clean: bool
    kill_switch_clear: bool = True
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.execution_mode not in {"PAPER","SHADOW"}:
            raise ValueError("SHARED03_NONLIVE_MODE_REQUIRED")

    @property
    def context_fingerprint(self) -> str:
        return fingerprint({"execution_mode":self.execution_mode,
            "capital_state_fingerprint":self.capital_state.state_fingerprint,
            "reservation_fingerprint":None if self.reservation is None else self.reservation.reservation_fingerprint,
            "exposure_fingerprints":tuple(x.exposure_fingerprint for x in self.exposures),
            "market_data_fresh":self.market_data_fresh,"leverage_known":self.leverage_known,
            "reconciliation_clean":self.reconciliation_clean,"kill_switch_clear":self.kill_switch_clear,
            "schema_version":self.schema_version})

@dataclass(frozen=True, slots=True)
class Shared03GateResult:
    disposition: Shared03GateDisposition
    reason_codes: tuple[str, ...]

class Shared03NonLiveGate:
    """EQS-SHARED-03-v1.0 compatibility gate. It can only preserve or reduce authority."""
    _HALT_REASONS = {"CAPITAL_STATE_UNKNOWN","ACCOUNT_STATE_UNKNOWN","CAPITAL_RECONCILIATION_UNCLEAN",
        "CAPITAL_STATE_STALE_OR_UNBOUNDED","RESERVATION_EXECUTION_STATE_UNKNOWN",
        "RESERVATION_RECONCILIATION_UNCLEAN","MARKET_DATA_STALE","LEVERAGE_STATE_UNKNOWN",
        "RECONCILIATION_UNCLEAN","KILL_SWITCH_ACTIVE"}

    def evaluate(self, order: OrderRequest, context: Shared03Context) -> Shared03GateResult:
        reasons = list(context.capital_state.validate(order.decision_time))
        if not context.market_data_fresh: reasons.append("MARKET_DATA_STALE")
        if not context.leverage_known: reasons.append("LEVERAGE_STATE_UNKNOWN")
        if not context.reconciliation_clean: reasons.append("RECONCILIATION_UNCLEAN")
        if not context.kill_switch_clear: reasons.append("KILL_SWITCH_ACTIVE")
        if not context.exposures:
            reasons.append("EXPOSURE_CONTEXT_MISSING")
        else:
            for exposure in context.exposures: reasons.extend(exposure.validate())
            expected_venue = None if context.reservation is None else context.reservation.venue
            if not any(
                exposure.instrument == order.symbol
                and exposure.strategy_id == str(order.strategy_id)
                and (expected_venue is None or exposure.venue == expected_venue)
                for exposure in context.exposures
            ):
                reasons.append("EXPOSURE_ORDER_BINDING_MISSING")
        if context.reservation is None:
            reasons.append("RESERVATION_MISSING")
        else:
            reasons.extend(context.reservation.validate_for_new_risk(order, order.decision_time))
            if context.reservation.portfolio_id != context.capital_state.portfolio_id:
                reasons.append("RESERVATION_PORTFOLIO_MISMATCH")
            if context.reservation.venue and all(x.venue != context.reservation.venue for x in context.exposures):
                reasons.append("RESERVATION_EXPOSURE_VENUE_MISMATCH")
            if context.capital_state.reserved_capital is not None and context.capital_state.reserved_capital < context.reservation.remaining_notional:
                reasons.append("CAPITAL_RESERVED_BELOW_RESERVATION")
        reasons = sorted(set(reasons))
        if not reasons:
            return Shared03GateResult(Shared03GateDisposition.ALLOW, ("SHARED03_WITHIN_NONLIVE_CONTRACT",))
        disposition = Shared03GateDisposition.HALT if any(r in self._HALT_REASONS for r in reasons) else Shared03GateDisposition.BLOCK
        return Shared03GateResult(disposition, tuple(reasons))

