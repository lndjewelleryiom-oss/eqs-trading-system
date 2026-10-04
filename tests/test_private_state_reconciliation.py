from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.execution.adapter_contract import PrivateReadSnapshot
from quant_system.execution.broker import VenueOrder, VenueOrderStatus
from quant_system.execution.private_reconciliation import PrivateStateReconciler
from quant_system.execution.reconciliation import ReconciliationAction

NOW = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)

def snapshot(*, orders=(), positions=None, observed_at=NOW):
    return PrivateReadSnapshot(observed_at, positions or {}, tuple(orders))

def order(order_id):
    return VenueOrder(order_id, "OKX-1", "BTC-USDT-SWAP",
                      VenueOrderStatus.ACKNOWLEDGED, Decimal("1"))

def test_matching_durable_and_private_state_reconciles():
    oid = uuid4()
    result = PrivateStateReconciler().reconcile(
        snapshot=snapshot(orders=(order(oid),), positions={"BTC-USDT-SWAP": Decimal("2")}),
        expected_open_order_ids={oid},
        expected_positions={"BTC-USDT-SWAP": Decimal("2")},
        now=NOW,
    )
    assert result.action == ReconciliationAction.OK
    assert result.reasons == ("RECONCILED",)
def test_unknown_missing_and_position_mismatch_fail_closed():
    expected, unknown = uuid4(), uuid4()
    result = PrivateStateReconciler().reconcile(
        snapshot=snapshot(orders=(order(unknown),), positions={"BTC-USDT-SWAP": Decimal("3")}),
        expected_open_order_ids={expected},
        expected_positions={"BTC-USDT-SWAP": Decimal("2")},
        now=NOW,
    )
    assert result.action == ReconciliationAction.HALT
    assert "UNKNOWN_VENUE_ORDER" in result.reasons
    assert "MISSING_EXPECTED_OPEN_ORDER" in result.reasons
    assert "POSITION_MISMATCH" in result.reasons
    assert result.unknown_venue_orders == (unknown,)
    assert result.missing_venue_orders == (expected,)
    assert result.position_differences == {"BTC-USDT-SWAP": Decimal("1")}

def test_stale_private_state_halts_even_when_balances_match():
    result = PrivateStateReconciler(max_age_seconds=10).reconcile(
        snapshot=snapshot(observed_at=NOW - timedelta(seconds=11)),
        expected_open_order_ids=set(), expected_positions={}, now=NOW,
    )
    assert result.action == ReconciliationAction.HALT
    assert result.reasons == ("PRIVATE_STATE_STALE",)
def test_future_dated_private_state_halts_on_clock_skew():
    result = PrivateStateReconciler().reconcile(
        snapshot=snapshot(observed_at=NOW + timedelta(seconds=1)),
        expected_open_order_ids=set(), expected_positions={}, now=NOW,
    )
    assert result.action == ReconciliationAction.HALT
    assert result.reasons == ("PRIVATE_STATE_CLOCK_SKEW",)

def test_reconciler_has_no_exchange_mutation_methods():
    reconciler = PrivateStateReconciler()
    for name in ("submit_order", "cancel_order", "amend_order", "withdraw", "transfer"):
        assert not hasattr(reconciler, name)
