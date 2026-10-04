from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.core.enums import OrderType, Side
from quant_system.execution.models import OrderRequest


def test_order_ids_are_unique_by_default():
    now = datetime.now(timezone.utc)
    common = dict(
        strategy_id=uuid4(), symbol="XYZ", side=Side.BUY,
        quantity=Decimal("1"), order_type=OrderType.MARKET,
        decision_time=now, reference_price=Decimal("100"),
    )
    a = OrderRequest(**common)
    b = OrderRequest(**common)
    assert a.order_id != b.order_id

import pytest


def test_limit_price_must_be_positive():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValueError, match="limit_price must be positive"):
        OrderRequest(
            strategy_id=uuid4(), symbol="XYZ", side=Side.BUY,
            quantity=Decimal("1"), order_type=OrderType.LIMIT,
            decision_time=now, reference_price=Decimal("100"), limit_price=Decimal("0"),
        )
