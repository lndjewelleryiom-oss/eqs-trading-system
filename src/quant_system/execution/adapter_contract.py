from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from decimal import Decimal
from uuid import UUID
from quant_system.execution.models import OrderRequest

class NormalizedExecutionState(StrEnum):
    INTENT_CREATED="INTENT_CREATED"; VALIDATED="VALIDATED"; RISK_AUTHORISED="RISK_AUTHORISED"; ROUTED="ROUTED"
    SUBMISSION_PENDING="SUBMISSION_PENDING"; ACKNOWLEDGED="ACKNOWLEDGED"; PARTIALLY_FILLED="PARTIALLY_FILLED"; FILLED="FILLED"
    REJECTED="REJECTED"; CANCEL_PENDING="CANCEL_PENDING"; CANCELLED="CANCELLED"; EXPIRED="EXPIRED"; UNKNOWN="UNKNOWN"; RECONCILIATION_REQUIRED="RECONCILIATION_REQUIRED"

@dataclass(frozen=True, slots=True)
class VenueInstrument:
    symbol: str
    min_quantity: Decimal
    quantity_step: Decimal
    price_tick: Decimal | None = None
    trading_enabled: bool = True

@dataclass(frozen=True, slots=True)
class OrderValidation:
    valid: bool
    reasons: tuple[str,...]

@dataclass(frozen=True, slots=True)
class PrivateReadSnapshot:
    observed_at: datetime
    positions: dict[str, Decimal]
    open_orders: tuple[object,...]
    recent_fills: tuple[object,...] = ()

class ExchangeAdapterContract:
    """V1 shape only. Implementations must never include withdrawal capability."""
    venue: str
    trading_capable: bool = False
    def health(self): raise NotImplementedError
    def instrument(self, symbol: str) -> VenueInstrument: raise NotImplementedError
    def private_read(self) -> PrivateReadSnapshot: raise NotImplementedError
    def get_order(self, client_order_id: UUID): raise NotImplementedError
    def validate_order(self, order: OrderRequest, instrument: VenueInstrument) -> OrderValidation:
        reasons=[]
        if not instrument.trading_enabled: reasons.append("INSTRUMENT_TRADING_DISABLED")
        if order.quantity < instrument.min_quantity: reasons.append("QUANTITY_BELOW_MINIMUM")
        if order.quantity % instrument.quantity_step != 0: reasons.append("QUANTITY_STEP_MISMATCH")
        if order.limit_price is not None and instrument.price_tick is not None and order.limit_price % instrument.price_tick != 0: reasons.append("PRICE_TICK_MISMATCH")
        return OrderValidation(not reasons, tuple(reasons) or ("VALID",))
    def submit_order(self, order: OrderRequest): raise PermissionError("TRADING_NOT_ENABLED")
    def cancel_order(self, client_order_id: UUID): raise PermissionError("TRADING_NOT_ENABLED")
