from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from quant_system.backtest.simulator import SimulatedFill
from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import (
    BrokerGateway,
    InMemoryBrokerAdapter,
    SubmissionResult,
    VenueAccountSnapshot,
    VenueOrder,
    VenueOrderStatus,
)
from quant_system.execution.models import OrderRequest
from quant_system.execution.reconciliation import ExecutionReconciler
from quant_system.execution.v1 import (
    Capability,
    CompatibilityMappingError,
    CryptoOrderIntentContext,
    ExecutionEventType,
    ExecutionMode,
    InterlockIssuer,
    InterlockState,
    ReconciliationStatus,
    TimeInForce,
    connection_capability_from_gateway,
    execution_reconciliation_to_v1,
    order_request_to_order_intent,
    paper_fill_to_reservation_settlement,
    submission_interlock_from_gateway,
    submission_result_to_execution_event,
    venue_order_to_execution_event,
)
from quant_system.paper.engine import PaperFillRecord


NOW = datetime(2026, 9, 27, 18, 0, tzinfo=timezone.utc)
SEAL = "a" * 64


def _order(*, order_type=OrderType.MARKET, limit_price=None):
    return OrderRequest(
        strategy_id=uuid4(),
        symbol="BTCUSDT",
        side=Side.BUY,
        quantity=Decimal("0.01"),
        order_type=order_type,
        decision_time=NOW,
        reference_price=Decimal("65000"),
        limit_price=limit_price,
    )


def _context(mode=ExecutionMode.SHADOW, capability=Capability.PRIVATE_READ_ONLY):
    return CryptoOrderIntentContext(
        portfolio_decision_id="portfolio-decision-001",
        portfolio_id="crypto-portfolio",
        reservation_id="reservation-001",
        risk_authorisation_id="risk-auth-001",
        authorised_at=NOW - timedelta(seconds=1),
        expires_at=NOW + timedelta(minutes=5),
        venue_id="BYBIT",
        connection_id="bybit-main",
        required_capability=capability,
        mode=mode,
        programme_state_ref="programme-state-001",
        interlock_ref="interlock-001",
        market_state_ref="market-state-001",
        reference_data_ref="reference-data-001",
        instrument_id="BYBIT:LINEAR:BTCUSDT",
        currency="USDT",
        strategy_version="legacy-crypto-v1",
        time_in_force=TimeInForce.GTC,
        asset_extension={"product_type": "PERPETUAL"},
    )


def test_order_request_maps_to_canonical_v1_intent_without_mutating_legacy_order():
    order = _order()
    intent = order_request_to_order_intent(order, _context())
    assert intent.schema_version == "EQS-EXEC-ORDER-INTENT-v1.0"
    assert intent.asset_class.value == "CRYPTO"
    assert intent.instrument.venue_symbol == order.symbol
    assert intent.target.quantity == order.quantity
    assert intent.target.notional == order.notional
    assert intent.strategy.strategy_id == str(order.strategy_id)
    assert intent.mode == ExecutionMode.SHADOW
    assert intent.to_payload()["order"]["order_type"] == "MARKET"


def test_order_intent_mapping_is_deterministic_for_same_legacy_order_and_context():
    order = _order()
    first = order_request_to_order_intent(order, _context())
    second = order_request_to_order_intent(order, _context())
    assert first.execution_intent_id == second.execution_intent_id
    assert first.request_id == second.request_id


def test_live_intent_requires_explicit_trading_capability():
    with pytest.raises(ValueError, match="TRADING_CAPABLE"):
        order_request_to_order_intent(_order(), _context(ExecutionMode.LIVE, Capability.PRIVATE_READ_ONLY))


def test_capability_adapter_never_infers_trading_from_disabled_gateway():
    gateway = BrokerGateway(InMemoryBrokerAdapter(), venue_submission_enabled=False)
    capability = connection_capability_from_gateway(
        gateway,
        connection_id="bybit-main",
        venue_id="BYBIT",
        verified_capabilities=(Capability.PUBLIC_ONLY, Capability.PRIVATE_READ_ONLY),
        verified_at=NOW,
        verification_ref="capability-evidence-001",
    )
    assert not capability.production_submission_enabled
    assert Capability.TRADING_CAPABLE not in capability.capabilities


def test_submission_enabled_gateway_requires_explicit_trading_capability_evidence():
    gateway = BrokerGateway(InMemoryBrokerAdapter(), venue_submission_enabled=True)
    with pytest.raises(ValueError, match="TRADING_CAPABLE"):
        connection_capability_from_gateway(
            gateway,
            connection_id="fixture-conn",
            venue_id="FIXTURE",
            verified_capabilities=(Capability.PRIVATE_READ_ONLY,),
            verified_at=NOW,
            verification_ref="capability-evidence-002",
        )


def test_legacy_gateway_can_only_derive_nonlive_interlocks():
    gateway = BrokerGateway(InMemoryBrokerAdapter(), venue_submission_enabled=False)
    shadow = submission_interlock_from_gateway(
        gateway,
        mode=ExecutionMode.SHADOW,
        interlock_id="interlock-shadow-001",
        revision=1,
        issued_by=InterlockIssuer.EQS_00,
        valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=10),
        programme_state_ref="programme-state-001",
        seal_hash=SEAL,
    )
    assert shadow.state == InterlockState.SHADOW_ZERO_SUBMIT
    with pytest.raises(CompatibilityMappingError, match="cannot be derived"):
        submission_interlock_from_gateway(
            gateway,
            mode=ExecutionMode.LIVE,
            interlock_id="interlock-live-001",
            revision=1,
            issued_by=InterlockIssuer.EQS_00,
            valid_from=NOW - timedelta(minutes=1),
            valid_until=NOW + timedelta(minutes=10),
            programme_state_ref="programme-state-001",
            seal_hash=SEAL,
        )


def test_shadow_submission_suppression_maps_to_submission_attempt_not_external_fill_truth():
    order = _order()
    result = BrokerGateway(InMemoryBrokerAdapter(), venue_submission_enabled=False).submit(order)
    event = submission_result_to_execution_event(
        result,
        execution_id="execution-crypto-001",
        venue_id="BYBIT",
        receive_timestamp=NOW,
    )
    assert event.event_type == ExecutionEventType.SUBMISSION_ATTEMPT
    assert event.external_order_id is None
    assert len(event.payload_hash) == 64


def test_submission_exception_maps_fail_closed_to_unknown_outcome():
    order = _order()
    result = BrokerGateway(InMemoryBrokerAdapter(fail_submit=True), venue_submission_enabled=True).submit(order)
    event = submission_result_to_execution_event(
        result,
        execution_id="execution-crypto-002",
        venue_id="FIXTURE",
        receive_timestamp=NOW,
    )
    assert event.event_type == ExecutionEventType.UNKNOWN_SUBMISSION_OUTCOME


def test_legacy_venue_fill_cannot_be_promoted_without_fill_identity_and_price():
    venue_order = VenueOrder(uuid4(), "V-1", "BTCUSDT", VenueOrderStatus.FILLED, Decimal("1"), Decimal("1"))
    with pytest.raises(CompatibilityMappingError, match="cannot fabricate canonical fill truth"):
        venue_order_to_execution_event(
            venue_order,
            execution_id="execution-crypto-003",
            venue_id="BYBIT",
            receive_timestamp=NOW,
        )


def test_legacy_reconciliation_maps_to_v1_unresolved_records():
    expected = uuid4()
    unknown = uuid4()
    snapshot = VenueAccountSnapshot(
        (VenueOrder(unknown, "V-X", "BTCUSDT", VenueOrderStatus.ACKNOWLEDGED, Decimal("1")),),
        {"BTCUSDT": Decimal("3")},
        NOW,
    )
    legacy = ExecutionReconciler().reconcile(
        expected_open_order_ids={expected},
        expected_positions={"BTCUSDT": Decimal("2")},
        venue=snapshot,
    )
    result = execution_reconciliation_to_v1(
        legacy,
        venue_snapshot=snapshot,
        expected_open_order_ids={expected},
        venue_id="BYBIT",
        connection_id="bybit-main",
        started_at=NOW,
        completed_at=NOW + timedelta(milliseconds=1),
    )
    assert result.status == ReconciliationStatus.RECONCILIATION_REQUIRED
    assert result.counts.unknown_orders == 1
    assert {item.kind.value for item in result.unresolved} == {"ORDER", "POSITION"}


def test_paper_fill_maps_to_reservation_settlement_without_becoming_venue_truth():
    order = _order()
    fill = SimulatedFill(
        order_id=order.order_id,
        symbol=order.symbol,
        quantity=Decimal("0.005"),
        price=Decimal("65100"),
        commission=Decimal("0.10"),
        timestamp=NOW + timedelta(seconds=1),
    )
    settlement = paper_fill_to_reservation_settlement(
        PaperFillRecord(order, fill),
        reservation_id="reservation-001",
        execution_id="execution-crypto-004",
    )
    assert settlement.event.value == "PARTIAL_FILL"
    assert settlement.remaining_quantity == Decimal("0.005")
    assert settlement.actual_notional == Decimal("325.500")
    assert settlement.execution_truth_ref is None
