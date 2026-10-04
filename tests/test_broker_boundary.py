from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest


def order():
    return OrderRequest(uuid4(), "ABC", Side.BUY, Decimal("10"), OrderType.MARKET, datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("100"))


def test_gateway_does_not_touch_adapter_when_submission_disabled():
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter)
    result = gateway.submit(order())
    assert not result.sent
    assert result.reason == "VENUE_SUBMISSION_DISABLED"
    assert adapter.submitted == []


def test_unhealthy_broker_halts_gateway_before_submission():
    adapter = InMemoryBrokerAdapter(healthy=False)
    gateway = BrokerGateway(adapter, venue_submission_enabled=True)
    result = gateway.submit(order())
    assert not result.sent
    assert gateway.halted
    assert result.reason.startswith("BROKER_UNHEALTHY")
    assert adapter.submitted == []


def test_submission_exception_halts_gateway():
    adapter = InMemoryBrokerAdapter(fail_submit=True)
    gateway = BrokerGateway(adapter, venue_submission_enabled=True)
    result = gateway.submit(order())
    assert not result.sent
    assert gateway.halted
    assert "SUBMISSION_FAILURE" in result.reason
