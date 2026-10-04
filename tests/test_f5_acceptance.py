"""F5 exit: broker-facing path with venue submission disabled survives faults safely."""

from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter, VenueOrder, VenueOrderStatus
from quant_system.execution.models import OrderRequest
from quant_system.execution.reconciliation import ExecutionReconciler, ReconciliationAction
from quant_system.shadow import ShadowExecutionEngine


def make_order():
    return OrderRequest(uuid4(), "ABC", Side.BUY, Decimal("5"), OrderType.MARKET, datetime(2026, 1, 1, tzinfo=timezone.utc), Decimal("100"))


def test_disabled_submission_path_and_fault_containment():
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=False)
    shadow = ShadowExecutionEngine(gateway)

    decision = shadow.evaluate_order(make_order())
    assert not decision.submission_result.sent
    assert adapter.submitted == []

    unknown_id = uuid4()
    adapter.open_orders[unknown_id] = VenueOrder(unknown_id, "UNEXPECTED", "XYZ", VenueOrderStatus.ACKNOWLEDGED, Decimal("1"))
    reconciler = ExecutionReconciler()
    reconciliation = reconciler.reconcile(expected_open_order_ids=set(), expected_positions={}, venue=adapter.account_snapshot())
    assert reconciliation.action == ReconciliationAction.HALT
    reconciler.contain_unknown_orders(reconciliation, gateway=gateway)
    assert gateway.halted
    assert unknown_id in adapter.canceled

    # Once halted, even a later healthy order remains blocked until an external recovery procedure acts.
    second = gateway.submit(make_order())
    assert not second.sent
    assert second.reason.startswith("HALTED:")
    assert adapter.submitted == []
