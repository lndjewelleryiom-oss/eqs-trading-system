from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter, VenueAccountSnapshot, VenueOrder, VenueOrderStatus
from quant_system.execution.reconciliation import ExecutionReconciler, ReconciliationAction


def test_matching_broker_state_reconciles():
    oid = uuid4()
    venue = VenueAccountSnapshot((VenueOrder(oid, "V1", "ABC", VenueOrderStatus.ACKNOWLEDGED, Decimal("1")),), {"ABC": Decimal("2")}, datetime.now(timezone.utc))
    result = ExecutionReconciler().reconcile(expected_open_order_ids={oid}, expected_positions={"ABC": Decimal("2")}, venue=venue)
    assert result.action == ReconciliationAction.OK


def test_unknown_order_and_position_mismatch_halt():
    expected = uuid4()
    unknown = uuid4()
    venue = VenueAccountSnapshot((VenueOrder(unknown, "V-X", "ABC", VenueOrderStatus.ACKNOWLEDGED, Decimal("1")),), {"ABC": Decimal("3")}, datetime.now(timezone.utc))
    result = ExecutionReconciler().reconcile(expected_open_order_ids={expected}, expected_positions={"ABC": Decimal("2")}, venue=venue)
    assert result.action == ReconciliationAction.HALT
    assert "UNKNOWN_VENUE_ORDER" in result.reasons
    assert "MISSING_EXPECTED_OPEN_ORDER" in result.reasons
    assert "POSITION_MISMATCH" in result.reasons


def test_unknown_order_containment_halts_gateway_and_cancels_unknown():
    unknown = uuid4()
    adapter = InMemoryBrokerAdapter()
    adapter.open_orders[unknown] = VenueOrder(unknown, "V-X", "ABC", VenueOrderStatus.ACKNOWLEDGED, Decimal("1"))
    gateway = BrokerGateway(adapter)
    reconciler = ExecutionReconciler()
    result = reconciler.reconcile(expected_open_order_ids=set(), expected_positions={}, venue=adapter.account_snapshot())
    reconciler.contain_unknown_orders(result, gateway=gateway)
    assert gateway.halted
    assert unknown in adapter.canceled
    assert unknown not in adapter.open_orders
