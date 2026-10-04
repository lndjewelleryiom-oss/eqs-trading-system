from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

from quant_system.core.enums import OrderType, Side
from quant_system.data.crypto_perps.models import PerpetualInstrumentDefinition
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter, VenueOrder, VenueOrderStatus
from quant_system.execution.models import OrderRequest
from quant_system.execution.v1 import (
    AdapterErrorCategory,
    Capability,
    CryptoBrokerAdapterBridge,
    CryptoOrderIntentContext,
    ExecutionMode,
    TimeInForce,
    connection_capability_from_gateway,
    crypto_perpetual_definition_to_constraint_snapshot,
    order_request_to_order_intent,
    validate_order_intent_against_constraints,
)

NOW = datetime(2026, 9, 27, 19, 0, tzinfo=timezone.utc)
RAW = "a" * 64


def _definition(*, status="TRADING", tick="0.10", lot="0.001"):
    return PerpetualInstrumentDefinition(
        instrument_id="BTC-USDT-PERP:BINANCE_USDM",
        venue="BINANCE_USDM",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settle_asset="USDT",
        contract_style="LINEAR",
        tick_size=Decimal(tick),
        lot_size=Decimal(lot),
        contract_value=None,
        status=status,
        effective_from=NOW - timedelta(days=1000),
        published_at=NOW - timedelta(minutes=2),
        available_at=NOW - timedelta(minutes=1),
        received_at=NOW - timedelta(minutes=1),
        raw_sha256=RAW,
    )


def _capability(adapter, *, trading=False):
    gateway = BrokerGateway(adapter, venue_submission_enabled=trading)
    caps = (Capability.PUBLIC_ONLY, Capability.PRIVATE_READ_ONLY)
    if trading:
        caps = caps + (Capability.TRADING_CAPABLE,)
    return connection_capability_from_gateway(
        gateway,
        connection_id="binance-usdm-main",
        venue_id="BINANCE_USDM",
        verified_capabilities=caps,
        verified_at=NOW - timedelta(seconds=10),
        verification_ref="capability-proof-001",
    )


def _bridge(*, adapter=None, trading=False, valid_for=timedelta(minutes=5)):
    adapter = adapter or InMemoryBrokerAdapter()
    definition = _definition()
    snapshot = crypto_perpetual_definition_to_constraint_snapshot(
        definition,
        valid_until=definition.available_at + valid_for,
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
    )
    return CryptoBrokerAdapterBridge(
        adapter,
        connection_capability=_capability(adapter, trading=trading),
        constraint_snapshots={snapshot.instrument_id: snapshot},
        symbol_to_instrument_id={"BTCUSDT": snapshot.instrument_id},
    ), snapshot


def _intent(*, quantity="0.010", reference_price="65000", limit_price=None):
    order = OrderRequest(
        strategy_id=uuid4(), symbol="BTCUSDT", side=Side.BUY,
        quantity=Decimal(quantity), order_type=OrderType.LIMIT if limit_price else OrderType.MARKET,
        decision_time=NOW, reference_price=Decimal(reference_price),
        limit_price=Decimal(limit_price) if limit_price else None,
    )
    context = CryptoOrderIntentContext(
        portfolio_decision_id="portfolio-decision-001", portfolio_id="crypto-portfolio",
        reservation_id="reservation-001", risk_authorisation_id="risk-auth-001",
        authorised_at=NOW - timedelta(seconds=2), expires_at=NOW + timedelta(minutes=5),
        venue_id="BINANCE_USDM", connection_id="binance-usdm-main",
        required_capability=Capability.PRIVATE_READ_ONLY, mode=ExecutionMode.SHADOW,
        programme_state_ref="programme-state-001", interlock_ref="interlock-001",
        market_state_ref="market-state-001", reference_data_ref="reference-data-001",
        instrument_id="BTC-USDT-PERP:BINANCE_USDM", currency="USDT",
        strategy_version="legacy-crypto-v1", time_in_force=TimeInForce.GTC,
        asset_extension={"product_type": "PERPETUAL"},
    )
    return order_request_to_order_intent(order, context)


def test_crypto_definition_maps_to_immutable_canonical_constraint_snapshot():
    definition = _definition()
    snapshot = crypto_perpetual_definition_to_constraint_snapshot(definition, valid_until=NOW + timedelta(minutes=5))
    assert snapshot.schema_version == "EQS-EXEC-CONSTRAINT-SNAPSHOT-v1.0"
    assert snapshot.tick_size == Decimal("0.10")
    assert snapshot.lot_size == Decimal("0.001")
    assert snapshot.asset_extension["product_type"] == "PERPETUAL"
    assert snapshot.asset_extension["contract_style"] == "LINEAR"
    assert snapshot.source_ref == f"sha256:{RAW}"
    assert len(snapshot.payload_hash) == 64
    assert snapshot.payload_hash == crypto_perpetual_definition_to_constraint_snapshot(definition, valid_until=NOW + timedelta(minutes=5)).payload_hash


def test_constraint_validation_accepts_aligned_crypto_order():
    _, snapshot = _bridge()
    result = validate_order_intent_against_constraints(_intent(), snapshot, checked_at=NOW)
    assert result.accepted
    assert result.violations == ()
    assert result.snapshot_hash == snapshot.payload_hash


def test_constraint_validation_rejects_bad_lot_and_bad_tick():
    _, snapshot = _bridge()
    result = validate_order_intent_against_constraints(_intent(quantity="0.0105", limit_price="65000.05"), snapshot, checked_at=NOW)
    assert not result.accepted
    assert "QUANTITY_NOT_LOT_ALIGNED" in result.violations
    assert "LIMIT_PRICE_NOT_TICK_ALIGNED" in result.violations


def test_stale_constraint_snapshot_fails_closed():
    bridge, snapshot = _bridge(valid_for=timedelta(seconds=30))
    result = bridge.get_instrument_constraints(snapshot.instrument_id, at=NOW)
    assert not result.ok
    assert result.error.category == AdapterErrorCategory.STALE_STATE


def test_unknown_constraints_fail_closed():
    bridge, _ = _bridge()
    result = bridge.get_instrument_constraints("ETH-USDT-PERP:BINANCE_USDM", at=NOW)
    assert not result.ok
    assert result.error.category == AdapterErrorCategory.NOT_FOUND


def test_crypto_venue_and_session_state_normalize_existing_adapter():
    bridge, snapshot = _bridge()
    venue = bridge.get_venue_state(at=NOW)
    session = bridge.get_market_session_state(snapshot.instrument_id, at=NOW)
    assert venue.ok and venue.value.state.value == "TRADING"
    assert session.ok and session.value.state.value == "OPEN"
    assert session.value.session_label == "CRYPTO_24X7"


def test_private_account_state_and_open_order_are_normalized():
    legacy = InMemoryBrokerAdapter()
    order_id = uuid4()
    legacy.open_orders[order_id] = VenueOrder(order_id, "V-1", "BTCUSDT", VenueOrderStatus.ACKNOWLEDGED, Decimal("0.01"))
    legacy.positions["BTCUSDT"] = Decimal("0.02")
    bridge, _ = _bridge(adapter=legacy)
    account = bridge.get_account_state(at=NOW)
    assert account.ok
    assert account.value.positions[0].instrument_id == "BTC-USDT-PERP:BINANCE_USDM"
    assert account.value.open_orders[0].external_order_id == "V-1"
    looked_up = bridge.get_order(str(order_id), at=NOW)
    assert looked_up.ok and looked_up.value.client_order_id == str(order_id)


def test_recent_fills_are_not_fabricated_when_legacy_adapter_cannot_supply_them():
    bridge, _ = _bridge()
    result = bridge.list_recent_fills(at=NOW)
    assert not result.ok
    assert result.error.category == AdapterErrorCategory.UNSUPPORTED
    assert result.error.code == "RECENT_FILLS_UNAVAILABLE"


def test_nonlive_bridge_cannot_submit_or_cancel_and_has_zero_side_effects():
    legacy = InMemoryBrokerAdapter()
    bridge, _ = _bridge(adapter=legacy)
    submit = bridge.submit_order(_intent(), action_id="action-submit-001", at=NOW)
    cancel = bridge.cancel_order(str(uuid4()), action_id="action-cancel-001", at=NOW)
    assert not submit.ok and submit.error.category == AdapterErrorCategory.TRADING_DISABLED
    assert not cancel.ok and cancel.error.category == AdapterErrorCategory.TRADING_DISABLED
    assert legacy.submitted == []
    assert legacy.canceled == []


def test_unhealthy_private_read_normalizes_transport_error_without_side_effect():
    legacy = InMemoryBrokerAdapter(healthy=False)
    bridge, _ = _bridge(adapter=legacy)
    venue = bridge.get_venue_state(at=NOW)
    account = bridge.get_account_state(at=NOW)
    assert venue.ok and venue.value.state.value == "DEGRADED"
    assert not account.ok and account.error.category == AdapterErrorCategory.TRANSPORT
    assert account.error.retryable


def test_trading_capable_fixture_still_enforces_constraints_before_legacy_submit():
    legacy = InMemoryBrokerAdapter()
    bridge, _ = _bridge(adapter=legacy, trading=True)
    bad = bridge.submit_order(_intent(quantity="0.0105"), action_id="action-submit-bad-001", at=NOW)
    assert not bad.ok and bad.error.category == AdapterErrorCategory.CONSTRAINT_VIOLATION
    assert legacy.submitted == []


def test_trading_capable_fixture_uses_deterministic_external_action_identity():
    legacy = InMemoryBrokerAdapter()
    bridge, _ = _bridge(adapter=legacy, trading=True)
    result = bridge.submit_order(_intent(), action_id="action-submit-good-001", at=NOW)
    assert result.ok
    assert len(legacy.submitted) == 1
    first_id = legacy.submitted[0].order_id
    legacy2 = InMemoryBrokerAdapter()
    bridge2, _ = _bridge(adapter=legacy2, trading=True)
    result2 = bridge2.submit_order(_intent(), action_id="action-submit-good-001", at=NOW)
    assert result2.ok
    assert legacy2.submitted[0].order_id == first_id


def test_constraint_asset_extension_is_immutable_and_source_versioned():
    _, snapshot = _bridge()
    assert snapshot.source_version == "crypto-perps-instrument-v1"
    try:
        snapshot.asset_extension["product_type"] = "SPOT"
        raised = False
    except TypeError:
        raised = True
    assert raised


def test_rate_limit_exception_normalizes_cross_asset_retry_semantics():
    from quant_system.execution.v1 import normalize_adapter_exception

    class RateLimitError(RuntimeError):
        status_code = 429
        retry_after = 2
        code = "TOO_MANY_REQUESTS"

    error = normalize_adapter_exception(RateLimitError("rate limit exceeded"))
    assert error.category == AdapterErrorCategory.RATE_LIMIT
    assert error.retryable
    assert error.retry_after_seconds == Decimal("2")
    assert error.external_code == "TOO_MANY_REQUESTS"
