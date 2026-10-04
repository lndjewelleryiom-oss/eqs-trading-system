from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest

BOUNDARY = "a3f0019dc42cbe7b26b9f3417e634456bff6e4a99dc7d1e0b5c86084990f5592"


def order():
    return OrderRequest(
        strategy_id=uuid4(), symbol="BTCUSDT", side=Side.BUY,
        quantity=Decimal("0.01"), order_type=OrderType.MARKET,
        decision_time=datetime.now(timezone.utc), reference_price=Decimal("1"),
    )
def test_enabled_broker_still_rejects_infrastructure_lineage():
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=True)
    result = gateway.submit(
        order(), lineage={"infrastructure_boundary_fingerprint": BOUNDARY}
    )
    assert result.sent is False
    assert result.reason == "INFRASTRUCTURE_ONLY_DATASET_BLOCKED"
    assert adapter.submitted == []


def test_unmarked_path_retains_existing_gateway_semantics():
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=False)
    result = gateway.submit(order())
    assert result.sent is False
    assert result.reason == "VENUE_SUBMISSION_DISABLED"
