from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import pytest

from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest
from quant_system.execution.v1 import (
    AdapterError,
    AdapterErrorCategory,
    AdapterResult,
    AssetClass,
    BalanceSnapshot,
    Capability,
    CanonicalVenueFee,
    CanonicalVenueFill,
    CanonicalVenueOrder,
    ConnectionCapability,
    ConstraintSnapshot,
    CryptoBrokerAdapterBridge,
    CryptoOrderIntentContext,
    DriftStatus,
    ExecutionMode,
    InterlockIssuer,
    InterlockState,
    MarketSessionState,
    PositionSnapshot,
    PrivateReadExpectation,
    PrivateTruthDomain,
    PrivateTruthState,
    ReconciliationStatus,
    SessionState,
    SubmissionInterlock,
    TimeInForce,
    TradingCommissioningBlocked,
    UnresolvedKind,
    VenueOperationalState,
    VenueState,
    assert_trading_capable_commissioning_ready,
    order_request_to_order_intent,
    reconcile_private_read,
)
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode, RuntimeStatus

NOW = datetime(2026, 9, 27, 20, 30, tzinfo=timezone.utc)
SEAL = "a" * 64


def capability(*, trading: bool = False) -> ConnectionCapability:
    caps = (Capability.PUBLIC_ONLY, Capability.PRIVATE_READ_ONLY)
    if trading:
        caps += (Capability.TRADING_CAPABLE,)
    return ConnectionCapability(
        connection_id="private-recon-connection", venue_id="CRYPTO-FIXTURE", capabilities=caps,
        verified_at=NOW - timedelta(seconds=5), verification_ref="private-recon-capability-proof",
        production_submission_enabled=False,
    )


def constraint() -> ConstraintSnapshot:
    return ConstraintSnapshot(
        snapshot_id="private-recon-constraint", asset_class=AssetClass.CRYPTO,
        instrument_id="CRYPTO:PERP:BTC", venue_id="CRYPTO-FIXTURE", venue_symbol="BTC-PERP",
        observed_at=NOW - timedelta(seconds=1), valid_until=NOW + timedelta(minutes=5),
        source_ref="private-recon-reference-data", source_version="fixture-v1", status="TRADING",
        tick_size=Decimal("1"), lot_size=Decimal("1"), asset_extension={"product_type": "PERPETUAL"},
    )


@dataclass
class CompletePrivateAdapter:
    connection_capability: ConnectionCapability
    orders: tuple[CanonicalVenueOrder, ...] = ()
    fills: tuple[CanonicalVenueFill, ...] = ()
    fees: tuple[CanonicalVenueFee, ...] = ()
    balances: tuple[BalanceSnapshot, ...] = ()
    positions: tuple[PositionSnapshot, ...] = ()
    observed_at: datetime = NOW

    def get_instrument_constraints(self, instrument_id: str, *, at: datetime):
        return AdapterResult.success(constraint(), at)

    def get_venue_state(self, *, at: datetime):
        return AdapterResult.success(VenueState("CRYPTO-FIXTURE", VenueOperationalState.TRADING, at), at)

    def get_market_session_state(self, instrument_id: str, *, at: datetime):
        return AdapterResult.success(MarketSessionState("CRYPTO-FIXTURE", instrument_id, SessionState.OPEN, at, "24X7"), at)

    def get_account_state(self, *, at: datetime):
        raise AssertionError("canonical reconciliation reads domains explicitly")

    def list_open_orders(self, *, at: datetime):
        return AdapterResult.success(self.orders, self.observed_at)

    def list_recent_fills(self, *, at: datetime):
        return AdapterResult.success(self.fills, self.observed_at)

    def list_recent_fees(self, *, at: datetime):
        return AdapterResult.success(self.fees, self.observed_at)

    def get_balances(self, *, at: datetime):
        return AdapterResult.success(self.balances, self.observed_at)

    def get_positions(self, *, at: datetime):
        return AdapterResult.success(self.positions, self.observed_at)

    def get_order(self, client_order_id: str, *, at: datetime):
        raise AssertionError("not used by reconciliation")

    def submit_order(self, intent, *, action_id: str, at: datetime):
        raise AssertionError("private reconciliation never submits")

    def cancel_order(self, client_order_id: str, *, action_id: str, at: datetime):
        raise AssertionError("private reconciliation never cancels")


def matching_adapter(*, trading=False, observed_at=NOW) -> CompletePrivateAdapter:
    return CompletePrivateAdapter(
        connection_capability=capability(trading=trading),
        orders=(CanonicalVenueOrder("client-order-001", "venue-order-001", "BTC-PERP", "ACKNOWLEDGED", Decimal("2"), Decimal("0")),),
        fills=(CanonicalVenueFill("fill-001", "client-order-000", "venue-order-000", "CRYPTO:PERP:BTC", Decimal("1"), Decimal("100"), observed_at),),
        fees=(CanonicalVenueFee("fee-001", "USD", Decimal("0.10"), observed_at, "COMMISSION", "fill-001", "client-order-000"),),
        balances=(BalanceSnapshot("USD", Decimal("1000"), Decimal("900"), Decimal("100"), observed_at),),
        positions=(PositionSnapshot("CRYPTO:PERP:BTC", Decimal("1")),),
        observed_at=observed_at,
    )


def expectation() -> PrivateReadExpectation:
    return PrivateReadExpectation(
        open_order_ids=frozenset({"client-order-001"}), fill_ids=frozenset({"fill-001"}),
        positions={"CRYPTO:PERP:BTC": Decimal("1")}, fees_by_currency={"USD": Decimal("0.10")},
        balances_by_asset={"USD": Decimal("1000")},
    )


def paper_engine() -> PaperTradingEngine:
    assumptions = ExecutionAssumptions(
        commission_bps=Decimal("1"), spread_bps=Decimal("2"), slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"), financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("0"), latency_ms=0,
    )
    return PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions), initial_cash=Decimal("100000"))


def legacy_order() -> OrderRequest:
    return OrderRequest(
        strategy_id=uuid4(), symbol="BTC-PERP", side=Side.BUY, quantity=Decimal("1"),
        order_type=OrderType.MARKET, decision_time=NOW, reference_price=Decimal("100"),
    )


def paper_intent(order: OrderRequest):
    context = CryptoOrderIntentContext(
        portfolio_decision_id="portfolio-decision-private-recon", portfolio_id="portfolio-private-recon",
        reservation_id="reservation-private-recon", risk_authorisation_id="risk-auth-private-recon",
        authorised_at=NOW - timedelta(seconds=2), expires_at=NOW + timedelta(minutes=5),
        venue_id="CRYPTO-FIXTURE", connection_id="private-recon-connection",
        required_capability=Capability.TRADING_CAPABLE, mode=ExecutionMode.PAPER,
        programme_state_ref="programme-state-private-recon", interlock_ref="interlock-private-recon",
        market_state_ref="market-state-private-recon", reference_data_ref="reference-data-private-recon",
        instrument_id="CRYPTO:PERP:BTC", currency="USD", strategy_version="fixture-v1",
        time_in_force=TimeInForce.GTC, asset_extension={"product_type": "PERPETUAL"},
    )
    return order_request_to_order_intent(order, context)


def paper_interlock() -> SubmissionInterlock:
    return SubmissionInterlock(
        interlock_id="interlock-private-recon", revision=1, state=InterlockState.PAPER_ONLY,
        issued_by=InterlockIssuer.EQS_00, valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=10), programme_state_ref="programme-state-private-recon",
        seal_hash=SEAL,
    )


def test_all_five_private_truth_domains_match_and_are_commissioning_ready():
    report = reconcile_private_read(matching_adapter(trading=True), expectation(), at=NOW)
    assert report.reconciliation.status == ReconciliationStatus.MATCHED
    assert report.commissioning_ready
    assert {item.domain for item in report.domains} == set(PrivateTruthDomain)
    assert all(item.state == PrivateTruthState.COMPLETE for item in report.domains)
    assert_trading_capable_commissioning_ready(capability(trading=True), report, at=NOW)


def test_domain_mismatches_cover_orders_fills_fees_balances_and_positions():
    adapter = CompletePrivateAdapter(connection_capability=capability(trading=True), observed_at=NOW)
    report = reconcile_private_read(adapter, expectation(), at=NOW)
    assert report.reconciliation.status == ReconciliationStatus.RECONCILIATION_REQUIRED
    assert not report.commissioning_ready
    kinds = {item.kind for item in report.reconciliation.unresolved}
    assert {UnresolvedKind.ORDER, UnresolvedKind.FILL, UnresolvedKind.FEE, UnresolvedKind.BALANCE, UnresolvedKind.POSITION} <= kinds
    with pytest.raises(TradingCommissioningBlocked):
        assert_trading_capable_commissioning_ready(capability(trading=True), report, at=NOW)


def test_stale_private_domain_is_blocked_unknown():
    report = reconcile_private_read(matching_adapter(trading=True, observed_at=NOW - timedelta(seconds=31)), expectation(), at=NOW)
    assert report.reconciliation.status == ReconciliationStatus.BLOCKED_UNKNOWN
    assert not report.commissioning_ready
    assert all(item.state == PrivateTruthState.STALE for item in report.domains)


def test_current_crypto_bridge_never_fabricates_missing_fill_fee_or_balance_truth():
    legacy = InMemoryBrokerAdapter()
    bridge = CryptoBrokerAdapterBridge(
        legacy, connection_capability=capability(trading=False), constraint_snapshots={constraint().instrument_id: constraint()},
        symbol_to_instrument_id={"BTC-PERP": "CRYPTO:PERP:BTC"}, clock=lambda: NOW,
    )
    report = reconcile_private_read(bridge, PrivateReadExpectation(), at=datetime.now(timezone.utc), max_age_seconds=30)
    states = {item.domain: item.state for item in report.domains}
    assert states[PrivateTruthDomain.ORDERS] == PrivateTruthState.COMPLETE
    assert states[PrivateTruthDomain.POSITIONS] == PrivateTruthState.COMPLETE
    assert states[PrivateTruthDomain.FILLS] == PrivateTruthState.UNSUPPORTED
    assert states[PrivateTruthDomain.FEES] == PrivateTruthState.UNSUPPORTED
    assert states[PrivateTruthDomain.BALANCES] == PrivateTruthState.UNSUPPORTED
    assert report.reconciliation.status == ReconciliationStatus.BLOCKED_UNKNOWN
    assert not report.commissioning_ready
    assert legacy.submitted == [] and legacy.canceled == []


def test_runtime_persists_private_reconciliation_and_restart_verifies_it(tmp_path):
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(runtime_id="private-recon-runtime", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    runtime.start()
    report = runtime.reconcile_v1_private_read(matching_adapter(), expectation())
    assert report.commissioning_ready
    stored = store.latest_v1_private_reconciliation("private-recon-runtime", venue_id="CRYPTO-FIXTURE", connection_id="private-recon-connection")
    assert stored is not None and stored.commissioning_ready
    assert store.verify_v1_private_reconciliation_journal("private-recon-runtime") == 1
    runtime.stop()
    resumed = PersistentPaperShadowRuntime(runtime_id="private-recon-runtime", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    assert resumed.start().status == RuntimeStatus.RUNNING


def test_trading_capable_predispatch_is_blocked_until_fresh_complete_private_truth_exists(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="commissioning-gate", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    runtime.start()
    adapter = matching_adapter(trading=True)
    order = legacy_order()
    intent = paper_intent(order)
    with pytest.raises(TradingCommissioningBlocked, match="requires durable private-read reconciliation"):
        runtime.submit_v1_order(order, intent=intent, capability=adapter.connection_capability, interlock=paper_interlock(), venue_adapter=adapter)
    assert runtime.status == RuntimeStatus.HALTED
    assert runtime.paper_engine.simulator.pending_count == 0


def test_fresh_complete_private_truth_unlocks_only_nonlive_paper_evaluation(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="commissioning-cleared", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    runtime.start()
    adapter = matching_adapter(trading=True)
    report = runtime.reconcile_v1_private_read(adapter, expectation())
    assert report.commissioning_ready and runtime.status == RuntimeStatus.RUNNING
    order = legacy_order()
    result = runtime.submit_v1_order(order, intent=paper_intent(order), capability=adapter.connection_capability, interlock=paper_interlock(), venue_adapter=adapter)
    assert result is not None
    assert runtime.paper_engine.simulator.pending_count == 1


def test_incomplete_trading_capable_reconciliation_halts_without_side_effects(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="commissioning-incomplete", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    runtime.start()
    adapter = CompletePrivateAdapter(connection_capability=capability(trading=True), observed_at=NOW)
    report = runtime.reconcile_v1_private_read(adapter, expectation())
    assert not report.commissioning_ready
    assert runtime.status == RuntimeStatus.HALTED
    assert runtime.paper_engine.simulator.pending_count == 0


def test_private_reconciliation_tamper_fails_closed_on_restart(tmp_path):
    import sqlite3
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(runtime_id="private-recon-tamper", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(), expectation())
    runtime.stop()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE execution_v1_private_reconciliations SET report_json='{}' WHERE runtime_id='private-recon-tamper'")
    resumed = PersistentPaperShadowRuntime(runtime_id="private-recon-tamper", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    with pytest.raises(Exception, match="private reconciliation"):
        resumed.start()
    assert store.get_runtime("private-recon-tamper").status == "HALTED"


def test_private_reconciliation_semantic_hash_detects_rehashed_database_tamper(tmp_path):
    import hashlib
    import json
    import sqlite3

    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(runtime_id="private-recon-semantic-tamper", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(), expectation())
    runtime.stop()
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT report_json FROM execution_v1_private_reconciliations WHERE runtime_id='private-recon-semantic-tamper'"
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["commissioning_ready"] = False
        rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        db_hash = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE execution_v1_private_reconciliations SET report_json=?, report_sha256=?, commissioning_ready=0 "
            "WHERE runtime_id='private-recon-semantic-tamper'",
            (rewritten, db_hash),
        )
    resumed = PersistentPaperShadowRuntime(runtime_id="private-recon-semantic-tamper", mode=RuntimeMode.PAPER, store=store, paper_engine=paper_engine(), clock=lambda: NOW)
    with pytest.raises(Exception, match="canonical payload hash"):
        resumed.start()



def changed_adapter(*, trading: bool, observed_at: datetime) -> CompletePrivateAdapter:
    return CompletePrivateAdapter(
        connection_capability=capability(trading=trading),
        orders=(CanonicalVenueOrder("client-order-002", "venue-order-002", "BTC-PERP", "ACKNOWLEDGED", Decimal("3"), Decimal("0")),),
        fills=(CanonicalVenueFill("fill-002", "client-order-010", "venue-order-010", "CRYPTO:PERP:BTC", Decimal("2"), Decimal("110"), observed_at),),
        fees=(CanonicalVenueFee("fee-002", "USD", Decimal("0.20"), observed_at, "COMMISSION", "fill-002", "client-order-010"),),
        balances=(BalanceSnapshot("USD", Decimal("1100"), Decimal("1000"), Decimal("100"), observed_at),),
        positions=(PositionSnapshot("CRYPTO:PERP:BTC", Decimal("2")),),
        observed_at=observed_at,
    )


def changed_expectation() -> PrivateReadExpectation:
    return PrivateReadExpectation(
        open_order_ids=frozenset({"client-order-002"}), fill_ids=frozenset({"fill-002"}),
        positions={"CRYPTO:PERP:BTC": Decimal("2")}, fees_by_currency={"USD": Decimal("0.20")},
        balances_by_asset={"USD": Decimal("1100")},
    )


def test_continuous_drift_detects_changes_across_all_five_domains(tmp_path):
    now = {"value": NOW}
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-all-domains", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: now["value"],
    )
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(trading=True, observed_at=NOW), expectation())
    baseline = store.latest_v1_private_drift(
        "drift-all-domains", venue_id="CRYPTO-FIXTURE", connection_id="private-recon-connection"
    )
    assert baseline is not None and baseline.status == DriftStatus.BASELINE_ESTABLISHED.value and not baseline.unresolved

    now["value"] = NOW + timedelta(seconds=1)
    report = runtime.reconcile_v1_private_read(
        changed_adapter(trading=True, observed_at=now["value"]), changed_expectation()
    )
    assert report.commissioning_ready
    drift = store.latest_v1_private_drift(
        "drift-all-domains", venue_id="CRYPTO-FIXTURE", connection_id="private-recon-connection"
    )
    assert drift is not None and drift.status == DriftStatus.DRIFT_DETECTED.value and drift.unresolved
    changed = {item["domain"] for item in drift.report["domains"] if item["changed"]}
    assert changed == {"ORDERS", "FILLS", "FEES", "BALANCES", "POSITIONS"}
    assert runtime.status == RuntimeStatus.RUNNING


def test_drift_requires_second_stable_reconciliation_before_commissioning_clears(tmp_path):
    now = {"value": NOW}
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-confirmation", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: now["value"],
    )
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(trading=True, observed_at=NOW), expectation())
    now["value"] = NOW + timedelta(seconds=1)
    adapter = changed_adapter(trading=True, observed_at=now["value"])
    runtime.reconcile_v1_private_read(adapter, changed_expectation())
    with pytest.raises(TradingCommissioningBlocked, match="unresolved private-account drift"):
        runtime._assert_v1_trading_commissioning_clearance(adapter.connection_capability, at=now["value"])

    now["value"] = NOW + timedelta(seconds=2)
    adapter = changed_adapter(trading=True, observed_at=now["value"])
    runtime.reconcile_v1_private_read(adapter, changed_expectation())
    drift = store.latest_v1_private_drift(
        "drift-confirmation", venue_id="CRYPTO-FIXTURE", connection_id="private-recon-connection"
    )
    assert drift is not None and drift.status == DriftStatus.DRIFT_RESOLVED.value and not drift.unresolved
    runtime._assert_v1_trading_commissioning_clearance(adapter.connection_capability, at=now["value"])

    order = legacy_order()
    result = runtime.submit_v1_order(
        order, intent=paper_intent(order), capability=adapter.connection_capability,
        interlock=paper_interlock(), venue_adapter=adapter,
    )
    assert result is not None
    assert runtime.paper_engine.simulator.pending_count == 1


def test_unresolved_drift_blocks_public_trading_capable_predispatch_without_paper_side_effect(tmp_path):
    now = {"value": NOW}
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-public-gate", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: now["value"],
    )
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(trading=True, observed_at=NOW), expectation())
    now["value"] = NOW + timedelta(seconds=1)
    adapter = changed_adapter(trading=True, observed_at=now["value"])
    runtime.reconcile_v1_private_read(adapter, changed_expectation())
    order = legacy_order()
    with pytest.raises(TradingCommissioningBlocked, match="unresolved private-account drift"):
        runtime.submit_v1_order(
            order, intent=paper_intent(order), capability=adapter.connection_capability,
            interlock=paper_interlock(), venue_adapter=adapter,
        )
    assert runtime.paper_engine.simulator.pending_count == 0
    assert runtime.status == RuntimeStatus.HALTED


def test_nontrading_capability_records_unresolved_drift_without_changing_nonlive_runtime_state(tmp_path):
    now = {"value": NOW}
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-nontrading", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: now["value"],
    )
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(trading=False, observed_at=NOW), expectation())
    now["value"] = NOW + timedelta(seconds=1)
    runtime.reconcile_v1_private_read(
        changed_adapter(trading=False, observed_at=now["value"]), changed_expectation()
    )
    drift = store.latest_v1_private_drift(
        "drift-nontrading", venue_id="CRYPTO-FIXTURE", connection_id="private-recon-connection"
    )
    assert drift is not None and drift.unresolved
    assert runtime.status == RuntimeStatus.RUNNING
    assert runtime.paper_engine.simulator.pending_count == 0


def test_private_drift_journal_survives_restart_and_is_verified(tmp_path):
    path = tmp_path / "runtime.db"
    now = {"value": NOW}
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-restart", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: now["value"],
    )
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(trading=True, observed_at=NOW), expectation())
    now["value"] = NOW + timedelta(seconds=1)
    runtime.reconcile_v1_private_read(
        changed_adapter(trading=True, observed_at=now["value"]), changed_expectation()
    )
    assert store.verify_v1_private_drift_journal("drift-restart") == 2
    runtime.stop()
    resumed = PersistentPaperShadowRuntime(
        runtime_id="drift-restart", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING


def test_private_drift_hash_tamper_fails_closed_on_restart(tmp_path):
    import sqlite3
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-hash-tamper", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(trading=False), expectation())
    runtime.stop()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE execution_v1_private_drift SET report_json='{}' WHERE runtime_id='drift-hash-tamper'")
    resumed = PersistentPaperShadowRuntime(
        runtime_id="drift-hash-tamper", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: NOW,
    )
    with pytest.raises(Exception, match="private drift"):
        resumed.start()
    assert store.get_runtime("drift-hash-tamper").status == "HALTED"


def test_private_drift_semantic_tamper_fails_even_when_database_and_payload_hashes_are_recomputed(tmp_path):
    import hashlib
    import json
    import sqlite3
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-semantic-tamper", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    runtime.reconcile_v1_private_read(matching_adapter(trading=False), expectation())
    runtime.stop()
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT report_json FROM execution_v1_private_drift WHERE runtime_id='drift-semantic-tamper'"
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["domains"][0]["current_count"] = 999
        base = dict(payload)
        base.pop("payload_hash", None)
        base_raw = json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        payload["payload_hash"] = hashlib.sha256(base_raw.encode("utf-8")).hexdigest()
        rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        db_hash = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()
        connection.execute(
            "UPDATE execution_v1_private_drift SET report_json=?, report_sha256=? WHERE runtime_id='drift-semantic-tamper'",
            (rewritten, db_hash),
        )
    resumed = PersistentPaperShadowRuntime(
        runtime_id="drift-semantic-tamper", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: NOW,
    )
    with pytest.raises(Exception, match="domain comparison mismatch"):
        resumed.start()


def test_missing_drift_assessment_blocks_trading_capable_commissioning(tmp_path):
    import sqlite3
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-required", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: NOW,
    )
    runtime.start()
    adapter = matching_adapter(trading=True)
    runtime.reconcile_v1_private_read(adapter, expectation())
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM execution_v1_private_drift WHERE runtime_id='drift-required'")
    with pytest.raises(TradingCommissioningBlocked, match="requires drift assessment"):
        runtime._assert_v1_trading_commissioning_clearance(adapter.connection_capability, at=NOW)



def test_first_drift_record_can_anchor_to_pre_drift_legacy_reconciliation(tmp_path):
    import sqlite3
    now = {"value": NOW}
    path = tmp_path / "runtime.db"
    store = PersistentRuntimeStore(path)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="drift-legacy-anchor", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), clock=lambda: now["value"],
    )
    runtime.start()
    first = runtime.reconcile_v1_private_read(matching_adapter(trading=False, observed_at=NOW), expectation())
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM execution_v1_private_drift WHERE runtime_id='drift-legacy-anchor'")
    now["value"] = NOW + timedelta(seconds=1)
    second = runtime.reconcile_v1_private_read(
        matching_adapter(trading=False, observed_at=now["value"]), expectation()
    )
    drift = store.latest_v1_private_drift(
        "drift-legacy-anchor", venue_id="CRYPTO-FIXTURE", connection_id="private-recon-connection"
    )
    assert drift is not None
    assert drift.previous_reconciliation_id == first.reconciliation.reconciliation_id
    assert drift.current_reconciliation_id == second.reconciliation.reconciliation_id
    assert drift.status == DriftStatus.STABLE.value
    assert store.verify_v1_private_drift_journal("drift-legacy-anchor") == 1
