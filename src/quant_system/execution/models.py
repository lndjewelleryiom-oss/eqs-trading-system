from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from quant_system.core.enums import OrderType, Side


@dataclass(frozen=True, slots=True)
class OrderRequest:
    strategy_id: UUID
    symbol: str
    side: Side
    quantity: Decimal
    order_type: OrderType
    decision_time: datetime
    reference_price: Decimal
    limit_price: Decimal | None = None
    reduce_only: bool = False
    order_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")
        if self.order_type == OrderType.LIMIT and self.limit_price is None:
            raise ValueError("limit orders require limit_price")
        if self.limit_price is not None and self.limit_price <= 0:
            raise ValueError("limit_price must be positive")
        if self.decision_time.tzinfo is None or self.decision_time.utcoffset() is None:
            raise ValueError("decision_time must be timezone-aware")

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.reference_price
