from decimal import Decimal
from uuid import uuid4
from quant_system.execution.broker import VenueAccountSnapshot, VenueOrder, VenueOrderStatus
from quant_system.execution.reconciliation import ExecutionReconciler, ReconciliationAction
from test_pretrade_authority import NOW

def test_unknown_venue_order_halts():
    unknown=VenueOrder(uuid4(),"v1","XYZ",VenueOrderStatus.ACKNOWLEDGED,Decimal("1"))
    result=ExecutionReconciler().reconcile(
        expected_open_order_ids=set(),expected_positions={},
        venue=VenueAccountSnapshot((unknown,),{},NOW))
    assert result.action == ReconciliationAction.HALT
    assert "UNKNOWN_VENUE_ORDER" in result.reasons

def test_missing_expected_order_halts():
    result=ExecutionReconciler().reconcile(
        expected_open_order_ids={uuid4()},expected_positions={},
        venue=VenueAccountSnapshot((),{},NOW))
    assert result.action == ReconciliationAction.HALT
    assert "MISSING_EXPECTED_OPEN_ORDER" in result.reasons

def test_position_mismatch_halts():
    result=ExecutionReconciler().reconcile(
        expected_open_order_ids=set(),expected_positions={"XYZ":Decimal("1")},
        venue=VenueAccountSnapshot((),{"XYZ":Decimal("2")},NOW))
    assert result.action == ReconciliationAction.HALT
    assert "POSITION_MISMATCH" in result.reasons
