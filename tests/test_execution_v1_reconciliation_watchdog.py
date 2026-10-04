from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import sqlite3

import pytest

from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import OrderType, Side
from quant_system.execution.models import OrderRequest
from quant_system.execution.v1 import (
    AdapterResult,
    AssetClass,
    BalanceSnapshot,
    Capability,
    CanonicalVenueFee,
    CanonicalVenueFill,
    CanonicalVenueOrder,
    ConnectionCapability,
    ConstraintSnapshot,
    CryptoOrderIntentContext,
    ExecutionMode,
    InterlockIssuer,
    InterlockState,
    MarketSessionState,
    PositionSnapshot,
    PrivateReadExpectation,
    ReconciliationCadencePolicy,
    ReconciliationWatchdogRecoveryPhase,
    ReconciliationWatchdogStatus,
    SessionState,
    SubmissionInterlock,
    TimeInForce,
    TradingCommissioningBlocked,
    VenueOperationalState,
    VenueState,
    order_request_to_order_intent,
)
from quant_system.execution.v1.watchdog import (
    CURRENT_WATCHDOG_SCHEMA_VERSION,
    HYSTERESIS_WATCHDOG_SCHEMA_VERSION,
    advance_recovery_context,
    assess_reconciliation_watchdog,
    hysteresis_v1_1_watchdog_payload,
    legacy_reconciliation_watchdog_payload,
    recovery_context_from_payload,
)
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import (
    FrozenWorkstreamExecutionRejected,
    PersistentPaperShadowRuntime,
    PersistentRuntimeStore,
    RuntimeConfig,
    RuntimeMode,
    RuntimeStatus,
    RuntimeLeaseError,
    WatchdogHeadConflictError,
)

NOW = datetime(2026, 9, 27, 21, 0, tzinfo=timezone.utc)
SEAL = "b" * 64


def capability(*, trading: bool = True) -> ConnectionCapability:
    caps = (Capability.PUBLIC_ONLY, Capability.PRIVATE_READ_ONLY)
    if trading:
        caps += (Capability.TRADING_CAPABLE,)
    return ConnectionCapability(
        connection_id="watchdog-connection", venue_id="CRYPTO-WATCHDOG", capabilities=caps,
        verified_at=NOW - timedelta(seconds=1), verification_ref="watchdog-capability-proof",
        production_submission_enabled=False,
    )


def constraint(at: datetime) -> ConstraintSnapshot:
    return ConstraintSnapshot(
        snapshot_id="watchdog-constraint", asset_class=AssetClass.CRYPTO,
        instrument_id="CRYPTO:PERP:BTC", venue_id="CRYPTO-WATCHDOG", venue_symbol="BTC-PERP",
        observed_at=at, valid_until=at + timedelta(minutes=10), source_ref="watchdog-reference",
        source_version="fixture-v1", status="TRADING", tick_size=Decimal("1"), lot_size=Decimal("1"),
        asset_extension={"product_type": "PERPETUAL"},
    )


@dataclass
class WatchdogAdapter:
    connection_capability: ConnectionCapability
    observed_at: datetime

    def get_instrument_constraints(self, instrument_id: str, *, at: datetime):
        return AdapterResult.success(constraint(at), at)

    def get_venue_state(self, *, at: datetime):
        return AdapterResult.success(VenueState("CRYPTO-WATCHDOG", VenueOperationalState.TRADING, at), at)

    def get_market_session_state(self, instrument_id: str, *, at: datetime):
        return AdapterResult.success(MarketSessionState("CRYPTO-WATCHDOG", instrument_id, SessionState.OPEN, at, "24X7"), at)

    def get_account_state(self, *, at: datetime):
        raise AssertionError("watchdog reconciliation reads explicit domains")

    def list_open_orders(self, *, at: datetime):
        value = (CanonicalVenueOrder("client-watchdog-001", "venue-watchdog-001", "BTC-PERP", "ACKNOWLEDGED", Decimal("1"), Decimal("0")),)
        return AdapterResult.success(value, self.observed_at)

    def list_recent_fills(self, *, at: datetime):
        value = (CanonicalVenueFill("fill-watchdog-001", "client-old", "venue-old", "CRYPTO:PERP:BTC", Decimal("1"), Decimal("100"), self.observed_at),)
        return AdapterResult.success(value, self.observed_at)

    def list_recent_fees(self, *, at: datetime):
        value = (CanonicalVenueFee("fee-watchdog-001", "USD", Decimal("0.10"), self.observed_at, "COMMISSION", "fill-watchdog-001", "client-old"),)
        return AdapterResult.success(value, self.observed_at)

    def get_balances(self, *, at: datetime):
        return AdapterResult.success((BalanceSnapshot("USD", Decimal("1000"), Decimal("900"), Decimal("100"), self.observed_at),), self.observed_at)

    def get_positions(self, *, at: datetime):
        return AdapterResult.success((PositionSnapshot("CRYPTO:PERP:BTC", Decimal("1")),), self.observed_at)

    def get_order(self, client_order_id: str, *, at: datetime):
        raise AssertionError("not used")

    def submit_order(self, intent, *, action_id: str, at: datetime):
        raise AssertionError("watchdog never submits")

    def cancel_order(self, client_order_id: str, *, action_id: str, at: datetime):
        raise AssertionError("watchdog never cancels")


def expectation() -> PrivateReadExpectation:
    return PrivateReadExpectation(
        open_order_ids=frozenset({"client-watchdog-001"}), fill_ids=frozenset({"fill-watchdog-001"}),
        positions={"CRYPTO:PERP:BTC": Decimal("1")}, fees_by_currency={"USD": Decimal("0.10")},
        balances_by_asset={"USD": Decimal("1000")},
    )


def paper_engine() -> PaperTradingEngine:
    assumptions = ExecutionAssumptions(
        commission_bps=Decimal("1"), spread_bps=Decimal("2"), slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"), financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("0"), latency_ms=0,
    )
    return PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions), initial_cash=Decimal("100000"))


def config() -> RuntimeConfig:
    return RuntimeConfig(
        v1_private_reconciliation_cadence_seconds=15,
        v1_private_reconciliation_watchdog_max_age_seconds=30,
    )


def order() -> OrderRequest:
    return OrderRequest(
        strategy_id=uuid4(), symbol="BTC-PERP", side=Side.BUY, quantity=Decimal("1"),
        order_type=OrderType.MARKET, decision_time=NOW, reference_price=Decimal("100"),
    )


def intent(value: OrderRequest):
    return order_request_to_order_intent(value, CryptoOrderIntentContext(
        portfolio_decision_id="watchdog-portfolio-decision", portfolio_id="watchdog-portfolio",
        reservation_id="watchdog-reservation", risk_authorisation_id="watchdog-risk-auth",
        authorised_at=NOW - timedelta(seconds=2), expires_at=NOW + timedelta(minutes=20),
        venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection",
        required_capability=Capability.TRADING_CAPABLE, mode=ExecutionMode.PAPER,
        programme_state_ref="watchdog-programme", interlock_ref="watchdog-interlock",
        market_state_ref="watchdog-market", reference_data_ref="watchdog-reference",
        instrument_id="CRYPTO:PERP:BTC", currency="USD", strategy_version="fixture-v1",
        time_in_force=TimeInForce.GTC, asset_extension={"product_type": "PERPETUAL"},
    ))


def interlock() -> SubmissionInterlock:
    return SubmissionInterlock(
        interlock_id="watchdog-interlock", revision=1, state=InterlockState.PAPER_ONLY,
        issued_by=InterlockIssuer.EQS_00, valid_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(minutes=30), programme_state_ref="watchdog-programme", seal_hash=SEAL,
    )


def runtime(tmp_path, now, *, trading=True):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    rt = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    rt.start()
    return rt, store, WatchdogAdapter(capability(trading=trading), now["value"])


def test_scheduler_runs_initial_reconciliation_then_exact_cadence(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    first = rt.run_v1_reconciliation_scheduler(adapter, expectation())
    assert first is not None and first.commissioning_ready
    policy = store.get_v1_reconciliation_policy("watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection")
    assert policy is not None and policy.cadence_seconds == 15 and policy.maximum_age_seconds == 30

    now["value"] = NOW + timedelta(seconds=14)
    adapter.observed_at = now["value"]
    assert rt.run_v1_reconciliation_scheduler(adapter, expectation()) is None
    now["value"] = NOW + timedelta(seconds=15)
    adapter.observed_at = now["value"]
    second = rt.run_v1_reconciliation_scheduler(adapter, expectation())
    assert second is not None and second.commissioning_ready
    assert len(store.list_v1_private_reconciliations("watchdog-runtime")) == 2


def test_heartbeat_automatically_places_commissioning_hold_after_missed_cadence(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    watchdog = store.latest_v1_reconciliation_watchdog("watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection")
    assert watchdog is not None
    assert watchdog.status == ReconciliationWatchdogStatus.MISSED_CADENCE.value
    assert watchdog.commissioning_hold
    assert rt.status == RuntimeStatus.RUNNING
    with pytest.raises(TradingCommissioningBlocked, match="watchdog hold:MISSED_CADENCE"):
        rt._assert_v1_trading_commissioning_clearance(adapter.connection_capability, at=now["value"])


def test_maximum_age_is_separate_hard_watchdog_state(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=31)
    rt.heartbeat()
    watchdog = store.latest_v1_reconciliation_watchdog("watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection")
    assert watchdog is not None
    assert watchdog.status == ReconciliationWatchdogStatus.MAX_AGE_EXCEEDED.value
    assert watchdog.commissioning_hold


def test_fresh_reconciliation_enters_recovery_hysteresis_until_later_healthy_follow_up(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    refreshed = rt.run_v1_reconciliation_scheduler(adapter, expectation())
    assert refreshed is not None and refreshed.commissioning_ready
    watchdog = store.latest_v1_reconciliation_watchdog("watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection")
    assert watchdog is not None and watchdog.status == ReconciliationWatchdogStatus.HEALTHY.value
    assert watchdog.commissioning_hold
    assert watchdog.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value
    with pytest.raises(TradingCommissioningBlocked, match="REQUIRE_HEALTHY_FOLLOW_UP"):
        rt._assert_v1_trading_commissioning_clearance(adapter.connection_capability, at=now["value"])

    now["value"] = NOW + timedelta(seconds=17)
    rt.heartbeat()
    confirmed = store.latest_v1_reconciliation_watchdog("watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection")
    assert confirmed is not None and confirmed.status == ReconciliationWatchdogStatus.HEALTHY.value
    assert not confirmed.commissioning_hold
    assert confirmed.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.NONE.value
    rt._assert_v1_trading_commissioning_clearance(adapter.connection_capability, at=now["value"])


def test_missed_watchdog_blocks_public_predispatch_without_paper_side_effect(tmp_path):
    now = {"value": NOW}
    rt, _, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    value = order()
    with pytest.raises(TradingCommissioningBlocked, match="watchdog hold:MISSED_CADENCE"):
        rt.submit_v1_order(value, intent=intent(value), capability=adapter.connection_capability, interlock=interlock(), venue_adapter=adapter)
    assert rt.paper_engine.simulator.pending_count == 0


def test_nontrading_runtime_records_missed_cadence_without_halting_nonlive_runtime(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now, trading=False)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    watchdog = store.latest_v1_reconciliation_watchdog("watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection")
    assert watchdog is not None and watchdog.commissioning_hold
    assert rt.status == RuntimeStatus.RUNNING


def test_watchdog_restart_verification_rejects_rehashed_semantic_tamper(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        raw = connection.execute("SELECT report_json FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence DESC LIMIT 1").fetchone()[0]
        payload = json.loads(raw)
        payload["commissioning_hold"] = True
        base = dict(payload)
        base.pop("payload_hash", None)
        payload["payload_hash"] = hashlib.sha256(json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        db_hash = hashlib.sha256(rewritten.encode()).hexdigest()
        connection.execute("UPDATE execution_v1_reconciliation_watchdog SET commissioning_hold=1, report_json=?, report_sha256=? WHERE runtime_id='watchdog-runtime'", (rewritten, db_hash))
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: NOW,
    )
    with pytest.raises(Exception, match="watchdog semantic assessment mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_policy_restart_verification_rejects_rehashed_semantic_tamper(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        raw = connection.execute("SELECT policy_json FROM execution_v1_reconciliation_policies WHERE runtime_id='watchdog-runtime'").fetchone()[0]
        payload = json.loads(raw)
        payload["cadence_seconds"] = 14
        base = dict(payload)
        base.pop("payload_hash", None)
        payload["payload_hash"] = hashlib.sha256(json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()
        rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        db_hash = hashlib.sha256(rewritten.encode()).hexdigest()
        connection.execute("UPDATE execution_v1_reconciliation_policies SET policy_json=?, policy_sha256=? WHERE runtime_id='watchdog-runtime'", (rewritten, db_hash))
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: NOW,
    )
    with pytest.raises(Exception, match="policy semantic mismatch"):
        resumed.start()


def test_restart_immediately_reassesses_and_holds_missed_cadence(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    now["value"] = NOW + timedelta(seconds=16)
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING
    watchdog = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert watchdog is not None
    assert watchdog.status == ReconciliationWatchdogStatus.MISSED_CADENCE.value
    assert watchdog.commissioning_hold


def test_same_timestamp_reassessment_cannot_satisfy_recovery_follow_up(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    before = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert before is not None and before.commissioning_hold
    count_before = len(store.list_v1_reconciliation_watchdog("watchdog-runtime"))
    rt.heartbeat()
    after = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert after is not None and after.assessment_id == before.assessment_id
    assert after.commissioning_hold
    assert len(store.list_v1_reconciliation_watchdog("watchdog-runtime")) == count_before


def test_max_age_breach_requires_fresh_reconciliation_and_healthy_follow_up(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=31)
    rt.heartbeat()
    breached = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert breached is not None and breached.status == ReconciliationWatchdogStatus.MAX_AGE_EXCEEDED.value
    assert breached.commissioning_hold

    adapter.observed_at = now["value"]
    refreshed = rt.run_v1_reconciliation_scheduler(adapter, expectation())
    assert refreshed is not None and refreshed.commissioning_ready
    pending = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert pending is not None and pending.commissioning_hold
    assert pending.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value

    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    cleared = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert cleared is not None and not cleared.commissioning_hold


def test_recovery_hysteresis_survives_restart_and_clears_only_on_post_restart_healthy_assessment(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    pending = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert pending is not None and pending.commissioning_hold
    rt.stop()

    now["value"] = NOW + timedelta(seconds=17)
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING
    cleared = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert cleared is not None and not cleared.commissioning_hold
    assert cleared.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.NONE.value


def test_rehashed_recovery_phase_tamper_fails_restart_verification(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT report_json FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()[0]
        payload = json.loads(raw)
        payload["recovery_phase"] = ReconciliationWatchdogRecoveryPhase.NONE.value
        payload["commissioning_hold"] = False
        base = dict(payload)
        base.pop("payload_hash", None)
        payload["payload_hash"] = hashlib.sha256(
            json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        ).hexdigest()
        rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        db_hash = hashlib.sha256(rewritten.encode()).hexdigest()
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET commissioning_hold=0, report_json=?, report_sha256=? WHERE runtime_id='watchdog-runtime' AND sequence=(SELECT MAX(sequence) FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime')",
            (rewritten, db_hash),
        )
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: NOW + timedelta(seconds=17),
    )
    with pytest.raises(Exception, match="watchdog semantic assessment mismatch"):
        resumed.start()


class NoTouchOptionsAdapter:
    connection_capability = capability(trading=False)

    def __getattribute__(self, name):
        if name == "connection_capability":
            return object.__getattribute__(self, name)
        raise AssertionError(f"EQS-06 frozen execution guard must reject before adapter access: {name}")


def test_eqs06_frozen_option_intent_rejected_before_adapter_or_external_action(tmp_path):
    from dataclasses import replace

    now = {"value": NOW}
    rt, store, _ = runtime(tmp_path, now, trading=False)
    value = order()
    option_intent = replace(intent(value), asset_class=AssetClass.OPTION)
    with pytest.raises(FrozenWorkstreamExecutionRejected, match="EQS00_EXECUTION_OPTIONS_DISABLED"):
        rt.submit_v1_order(
            value, intent=option_intent, capability=capability(trading=False),
            interlock=interlock(), venue_adapter=NoTouchOptionsAdapter(),
        )
    assert rt.status == RuntimeStatus.RUNNING
    assert store.list_v1_intent_bindings("watchdog-runtime") == ()
    assert store.list_v1_external_action_latest("watchdog-runtime") == ()
    events = store.load_events("watchdog-runtime")
    rejection = [item for item in events if item.event_type == "EQS00_EXECUTION_OPTIONS_DISABLED"]
    assert len(rejection) == 1
    assert rejection[0].payload["freeze_record"] == "EQS00-FRZ-EQS06-001"
    assert rejection[0].payload["external_action_permitted"] is False


def _rehash_watchdog_payload(payload: dict) -> tuple[str, str]:
    base = dict(payload)
    base.pop("payload_hash", None)
    payload["payload_hash"] = hashlib.sha256(
        json.dumps(base, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()
    rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return rewritten, hashlib.sha256(rewritten.encode()).hexdigest()


def test_restart_replay_keeps_breach_on_hold_until_fresh_reconciliation_then_later_follow_up(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    breached = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert breached is not None
    assert breached.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION.value
    rt.stop()

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING
    replayed_breach = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert replayed_breach is not None and replayed_breach.commissioning_hold
    assert replayed_breach.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION.value
    with pytest.raises(TradingCommissioningBlocked, match="REQUIRE_FRESH_RECONCILIATION"):
        resumed._assert_v1_trading_commissioning_clearance(adapter.connection_capability, at=now["value"])

    adapter.observed_at = now["value"]
    refreshed = resumed.run_v1_reconciliation_scheduler(adapter, expectation())
    assert refreshed is not None and refreshed.commissioning_ready
    pending = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert pending is not None and pending.commissioning_hold
    assert pending.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value
    resumed.stop()

    same_time = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert same_time.start().status == RuntimeStatus.RUNNING
    still_pending = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert still_pending is not None and still_pending.commissioning_hold
    assert still_pending.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value

    now["value"] += timedelta(seconds=1)
    same_time.heartbeat()
    cleared = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert cleared is not None and not cleared.commissioning_hold
    assert cleared.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.NONE.value


def test_recovery_breach_reset_requires_new_fresh_reconciliation_and_new_follow_up_after_restart(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    pending = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert pending is not None
    first_recovery_reconciliation_id = pending.report["recovery_reconciliation_id"]
    assert pending.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value

    now["value"] = NOW + timedelta(seconds=32)
    rt.heartbeat()
    reset = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert reset is not None and reset.commissioning_hold
    assert reset.status == ReconciliationWatchdogStatus.MISSED_CADENCE.value
    assert reset.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION.value
    assert reset.report["recovery_reconciliation_id"] is None
    assert reset.report["recovery_breach_at"] == (NOW + timedelta(seconds=32)).isoformat().replace("+00:00", "Z")
    assert reset.report["last_reconciliation_id"] == first_recovery_reconciliation_id
    rt.stop()

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING
    with pytest.raises(TradingCommissioningBlocked, match="REQUIRE_FRESH_RECONCILIATION"):
        resumed._assert_v1_trading_commissioning_clearance(adapter.connection_capability, at=now["value"])

    adapter.observed_at = now["value"]
    second_refresh = resumed.run_v1_reconciliation_scheduler(adapter, expectation())
    assert second_refresh is not None and second_refresh.commissioning_ready
    second_pending = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert second_pending is not None and second_pending.commissioning_hold
    assert second_pending.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value
    assert second_pending.report["recovery_reconciliation_id"] != first_recovery_reconciliation_id

    now["value"] += timedelta(seconds=1)
    resumed.heartbeat()
    cleared = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert cleared is not None and not cleared.commissioning_hold
    assert cleared.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.NONE.value


def test_tamper_replay_of_historical_breach_reset_is_rejected_even_when_rehashed(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=32)
    rt.heartbeat()
    rt.stop()

    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT sequence, report_json FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence"
        ).fetchall()
        reset_sequence, raw = rows[-1]
        payload = json.loads(raw)
        assert payload["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION.value
        payload["recovery_breach_at"] = (NOW + timedelta(seconds=16)).isoformat().replace("+00:00", "Z")
        payload["recovery_reconciliation_id"] = payload["last_reconciliation_id"]
        rewritten, db_hash = _rehash_watchdog_payload(payload)
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET report_json=?, report_sha256=? WHERE runtime_id='watchdog-runtime' AND sequence=?",
            (rewritten, db_hash, reset_sequence),
        )

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    with pytest.raises(Exception, match="watchdog semantic assessment mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_tamper_replay_cannot_skip_fresh_reconciliation_gate_by_rebinding_old_reconciliation(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    first = rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    rt.stop()

    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        sequence, raw = connection.execute(
            "SELECT sequence, report_json FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(raw)
        assert payload["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value
        payload["recovery_reconciliation_id"] = first.reconciliation.reconciliation_id
        rewritten, db_hash = _rehash_watchdog_payload(payload)
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET report_json=?, report_sha256=? WHERE runtime_id='watchdog-runtime' AND sequence=?",
            (rewritten, db_hash, sequence),
        )

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    with pytest.raises(Exception, match="watchdog semantic assessment mismatch"):
        resumed.start()


def test_tamper_replay_cannot_forge_later_healthy_follow_up_clearance(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    rt.stop()

    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        sequence, raw = connection.execute(
            "SELECT sequence, report_json FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(raw)
        assert payload["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value
        payload["commissioning_hold"] = False
        payload["recovery_phase"] = ReconciliationWatchdogRecoveryPhase.NONE.value
        payload["reason"] = "PRIVATE_RECONCILIATION_RECOVERY_CONFIRMED"
        rewritten, db_hash = _rehash_watchdog_payload(payload)
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET commissioning_hold=0, report_json=?, report_sha256=? WHERE runtime_id='watchdog-runtime' AND sequence=?",
            (rewritten, db_hash, sequence),
        )

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    with pytest.raises(Exception, match="watchdog semantic assessment mismatch"):
        resumed.start()


# Cross-version replay helpers intentionally rewrite only persisted watchdog records.
def _stored_watchdog_policy(
    store: PersistentRuntimeStore, *, venue_id: str = "CRYPTO-WATCHDOG", connection_id: str = "watchdog-connection"
) -> ReconciliationCadencePolicy:
    stored = store.get_v1_reconciliation_policy(
        "watchdog-runtime", venue_id=venue_id, connection_id=connection_id
    )
    assert stored is not None
    return ReconciliationCadencePolicy(
        policy_id=stored.policy_id, venue_id=stored.venue_id, connection_id=stored.connection_id,
        cadence_seconds=stored.cadence_seconds, maximum_age_seconds=stored.maximum_age_seconds,
        anchor_at=stored.anchor_at, trading_capable=stored.trading_capable,
    )


def _rebuild_watchdog_heads_for_fixture(path) -> None:
    """Fixture-only migration helper that emulates opening a pre-head-fencing database."""
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM execution_v1_reconciliation_watchdog_heads WHERE runtime_id='watchdog-runtime'")
        connection.execute(
            """INSERT INTO execution_v1_reconciliation_watchdog_heads(
            runtime_id, venue_id, connection_id, assessment_id, sequence, writer_owner_id, writer_generation, updated_at
            )
            SELECT w.runtime_id, w.venue_id, w.connection_id, w.assessment_id, w.sequence,
                   'MIGRATED', 0, w.assessed_at
            FROM execution_v1_reconciliation_watchdog AS w
            JOIN (
                SELECT runtime_id, venue_id, connection_id, MAX(sequence) AS max_sequence
                FROM execution_v1_reconciliation_watchdog
                WHERE runtime_id='watchdog-runtime'
                GROUP BY runtime_id, venue_id, connection_id
            ) AS latest
              ON latest.runtime_id=w.runtime_id
             AND latest.venue_id=w.venue_id
             AND latest.connection_id=w.connection_id
             AND latest.max_sequence=w.sequence
            WHERE w.runtime_id='watchdog-runtime'
            """
        )
        connection.commit()


def _repair_current_watchdog_predecessors(path) -> None:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT sequence, assessment_id, venue_id, connection_id, report_json "
            "FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence"
        ).fetchall()
        previous_by_scope = {}
        for sequence, assessment_id, venue_id, connection_id, raw in rows:
            scope = (str(venue_id), str(connection_id))
            payload = json.loads(raw)
            expected = previous_by_scope.get(scope)
            if payload.get("schema_version") == CURRENT_WATCHDOG_SCHEMA_VERSION:
                payload["predecessor_assessment_id"] = expected
                rewritten, db_hash = _rehash_watchdog_payload(payload)
                connection.execute(
                    "UPDATE execution_v1_reconciliation_watchdog SET report_json=?, report_sha256=? "
                    "WHERE runtime_id='watchdog-runtime' AND sequence=?",
                    (rewritten, db_hash, sequence),
                )
            previous_by_scope[scope] = str(assessment_id)
        connection.commit()


def _rewrite_watchdog_rows_as_v1_1(
    store: PersistentRuntimeStore, path, *, sequences: set[int] | None = None
) -> None:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT sequence, assessed_at, last_reconciliation_id, venue_id, connection_id, report_json "
            "FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence"
        ).fetchall()
        previous_by_scope = {}
        for sequence, assessed_at_raw, reconciliation_id, venue_id, connection_id, raw in rows:
            scope = (str(venue_id), str(connection_id))
            previous = previous_by_scope.get(scope)
            current_payload = json.loads(raw)
            if sequences is None or int(sequence) in sequences:
                policy = _stored_watchdog_policy(store, venue_id=str(venue_id), connection_id=str(connection_id))
                assessed_at = datetime.fromisoformat(str(assessed_at_raw).replace("Z", "+00:00"))
                recon = None if reconciliation_id is None else store.get_v1_private_reconciliation(
                    "watchdog-runtime", str(reconciliation_id)
                )
                current_payload = hysteresis_v1_1_watchdog_payload(
                    policy, assessed_at=assessed_at,
                    last_reconciliation_id=None if recon is None else recon.reconciliation_id,
                    last_reconciliation_completed_at=None if recon is None else recon.completed_at,
                    last_reconciliation_complete=False if recon is None else recon.commissioning_ready,
                    previous_assessment=previous,
                )
                rewritten = json.dumps(current_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
                db_hash = hashlib.sha256(rewritten.encode()).hexdigest()
                connection.execute(
                    "UPDATE execution_v1_reconciliation_watchdog SET assessment_id=?, next_due_at=?, status=?, "
                    "commissioning_hold=?, report_json=?, report_sha256=? WHERE runtime_id='watchdog-runtime' AND sequence=?",
                    (current_payload["assessment_id"], current_payload["next_due_at"], current_payload["status"],
                     int(current_payload["commissioning_hold"]), rewritten, db_hash, sequence),
                )
            previous_by_scope[scope] = advance_recovery_context(previous, current_payload)
        connection.commit()
    _repair_current_watchdog_predecessors(path)
    _rebuild_watchdog_heads_for_fixture(path)


def _rewrite_watchdog_rows_as_legacy_v1_0(
    store: PersistentRuntimeStore, path, *, sequences: set[int] | None = None
) -> None:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT sequence, assessment_id, assessed_at, last_reconciliation_id, venue_id, connection_id "
            "FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence"
        ).fetchall()
        selected = [row for row in rows if sequences is None or int(row[0]) in sequences]
        for sequence, assessment_id, _, _, _, _ in selected:
            connection.execute(
                "UPDATE execution_v1_reconciliation_watchdog SET assessment_id=? "
                "WHERE runtime_id='watchdog-runtime' AND sequence=?",
                (f"TEMP-LEGACY-{sequence}-{assessment_id}", sequence),
            )
        for sequence, _, assessed_at_raw, reconciliation_id, venue_id, connection_id in selected:
            policy = _stored_watchdog_policy(store, venue_id=str(venue_id), connection_id=str(connection_id))
            assessed_at = datetime.fromisoformat(str(assessed_at_raw).replace("Z", "+00:00"))
            recon = None if reconciliation_id is None else store.get_v1_private_reconciliation(
                "watchdog-runtime", str(reconciliation_id)
            )
            payload = legacy_reconciliation_watchdog_payload(
                policy, assessed_at=assessed_at,
                last_reconciliation_id=None if recon is None else recon.reconciliation_id,
                last_reconciliation_completed_at=None if recon is None else recon.completed_at,
            )
            rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            db_hash = hashlib.sha256(rewritten.encode()).hexdigest()
            connection.execute(
                "UPDATE execution_v1_reconciliation_watchdog "
                "SET assessment_id=?, next_due_at=?, status=?, commissioning_hold=?, report_json=?, report_sha256=? "
                "WHERE runtime_id='watchdog-runtime' AND sequence=?",
                (payload["assessment_id"], payload["next_due_at"], payload["status"],
                 int(payload["commissioning_hold"]), rewritten, db_hash, sequence),
            )
        connection.commit()
    _repair_current_watchdog_predecessors(path)
    _rebuild_watchdog_heads_for_fixture(path)


def test_cross_version_replay_accepts_legacy_v1_0_healthy_history_and_continues_v1_2(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path)

    now["value"] = NOW + timedelta(seconds=1)
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING
    history = store.list_v1_reconciliation_watchdog("watchdog-runtime")
    assert any(item.report["schema_version"] == "EQS-EXEC-RECON-WATCHDOG-v1.0" for item in history)
    latest = history[-1]
    assert latest.report["schema_version"] == "EQS-EXEC-RECON-WATCHDOG-v1.2"
    assert latest.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.NONE.value
    assert not latest.commissioning_hold


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected_status"),
    [
        (16, ReconciliationWatchdogStatus.MISSED_CADENCE),
        (31, ReconciliationWatchdogStatus.MAX_AGE_EXCEEDED),
    ],
)
def test_cross_version_replay_legacy_v1_0_breach_requires_current_fresh_reconciliation_and_follow_up(
    tmp_path, elapsed_seconds, expected_status
):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=elapsed_seconds)
    rt.heartbeat()
    rt.stop()
    path = tmp_path / "runtime.db"
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path)

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING
    breached = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert breached is not None and breached.status == expected_status.value
    assert breached.commissioning_hold
    assert breached.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION.value

    adapter.observed_at = now["value"]
    refreshed = resumed.reconcile_v1_private_read(adapter, expectation())
    assert refreshed.commissioning_ready
    pending = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert pending is not None and pending.commissioning_hold
    assert pending.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value

    now["value"] += timedelta(seconds=1)
    resumed.heartbeat()
    cleared = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert cleared is not None and not cleared.commissioning_hold
    assert cleared.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.NONE.value


def test_cross_version_replay_ambiguous_legacy_clearance_stays_fail_closed_until_new_current_recovery(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path)

    legacy_latest = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert legacy_latest is not None
    assert legacy_latest.report["schema_version"] == "EQS-EXEC-RECON-WATCHDOG-v1.0"
    assert legacy_latest.status == ReconciliationWatchdogStatus.HEALTHY.value
    assert not legacy_latest.commissioning_hold  # valid old semantics, ambiguous under v1.1 hysteresis

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING
    held = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert held is not None and held.commissioning_hold
    assert held.report["schema_version"] == "EQS-EXEC-RECON-WATCHDOG-v1.2"
    assert held.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION.value

    now["value"] += timedelta(seconds=1)
    adapter.observed_at = now["value"]
    resumed.reconcile_v1_private_read(adapter, expectation())
    pending = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert pending is not None and pending.commissioning_hold
    assert pending.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_HEALTHY_FOLLOW_UP.value
    now["value"] += timedelta(seconds=1)
    resumed.heartbeat()
    cleared = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert cleared is not None and not cleared.commissioning_hold


def test_cross_version_replay_rejects_rehashed_v1_2_to_v1_0_schema_downgrade(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    adapter.observed_at = now["value"]
    rt.run_v1_reconciliation_scheduler(adapter, expectation())
    rt.stop()
    history = store.list_v1_reconciliation_watchdog("watchdog-runtime")
    assert len(history) >= 2
    latest_sequence = history[-1].sequence
    path = tmp_path / "runtime.db"
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path, sequences={latest_sequence})

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    with pytest.raises(Exception, match="schema downgrade"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_cross_version_replay_rejects_ambiguous_unknown_watchdog_schema_even_when_rehashed(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        sequence, raw = connection.execute(
            "SELECT sequence, report_json FROM execution_v1_reconciliation_watchdog "
            "WHERE runtime_id='watchdog-runtime' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        payload = json.loads(raw)
        payload["schema_version"] = "EQS-EXEC-RECON-WATCHDOG-v1.0-AMBIGUOUS"
        rewritten, db_hash = _rehash_watchdog_payload(payload)
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET report_json=?, report_sha256=? "
            "WHERE runtime_id='watchdog-runtime' AND sequence=?",
            (rewritten, db_hash, sequence),
        )

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    with pytest.raises(Exception, match="unsupported schema version"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def _scoped_capability(venue_id: str, connection_id: str, *, trading: bool = True) -> ConnectionCapability:
    caps = (Capability.PUBLIC_ONLY, Capability.PRIVATE_READ_ONLY)
    if trading:
        caps += (Capability.TRADING_CAPABLE,)
    return ConnectionCapability(
        connection_id=connection_id,
        venue_id=venue_id,
        capabilities=caps,
        verified_at=NOW - timedelta(seconds=1),
        verification_ref=f"watchdog-capability-proof:{venue_id}:{connection_id}",
        production_submission_enabled=False,
    )


def _watchdog_sequences_for_scope(path, *, venue_id: str, connection_id: str) -> list[int]:
    with sqlite3.connect(path) as connection:
        return [
            int(row[0])
            for row in connection.execute(
                "SELECT sequence FROM execution_v1_reconciliation_watchdog "
                "WHERE runtime_id='watchdog-runtime' AND venue_id=? AND connection_id=? ORDER BY sequence",
                (venue_id, connection_id),
            ).fetchall()
        ]


def test_mixed_version_global_interleaving_is_valid_when_each_scope_is_monotonic(tmp_path):
    now = {"value": NOW}
    rt, store, adapter_a = runtime(tmp_path, now)
    adapter_b = WatchdogAdapter(_scoped_capability("CRYPTO-WATCHDOG-B", "watchdog-connection-b"), now["value"])
    rt.reconcile_v1_private_read(adapter_a, expectation())
    rt.reconcile_v1_private_read(adapter_b, expectation())
    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    rt.stop()

    path = tmp_path / "runtime.db"
    a_sequences = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    b_sequences = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG-B", connection_id="watchdog-connection-b"
    )
    assert len(a_sequences) >= 2 and len(b_sequences) >= 2

    # Global journal order intentionally becomes:
    #   A:v1.0, B:v1.0, A:v1.2, B:v1.0
    # which is valid because schema monotonicity is enforced per venue/connection scope.
    _rewrite_watchdog_rows_as_legacy_v1_0(
        store, path, sequences={a_sequences[0], *b_sequences}
    )

    with sqlite3.connect(path) as connection:
        ordered = [
            (row[1], row[2], json.loads(row[3])["schema_version"])
            for row in connection.execute(
                "SELECT sequence, venue_id, connection_id, report_json "
                "FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence"
            ).fetchall()
        ]
    assert any(v == "CRYPTO-WATCHDOG" and schema.endswith("v1.2") for v, _, schema in ordered)
    assert any(v == "CRYPTO-WATCHDOG-B" and schema.endswith("v1.0") for v, _, schema in ordered)

    now["value"] += timedelta(seconds=1)
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING

    history = store.list_v1_reconciliation_watchdog("watchdog-runtime")
    for venue_id, connection_id in (
        ("CRYPTO-WATCHDOG", "watchdog-connection"),
        ("CRYPTO-WATCHDOG-B", "watchdog-connection-b"),
    ):
        scoped = [
            item for item in history
            if item.venue_id == venue_id and item.connection_id == connection_id
        ]
        assert scoped[-1].report["schema_version"] == "EQS-EXEC-RECON-WATCHDOG-v1.2"
        assert scoped[-1].report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.NONE.value
        assert not scoped[-1].commissioning_hold


def test_mixed_version_restart_carries_legacy_breach_per_scope_without_contaminating_current_scope(tmp_path):
    now = {"value": NOW}
    rt, store, adapter_a = runtime(tmp_path, now)
    adapter_b = WatchdogAdapter(_scoped_capability("CRYPTO-WATCHDOG-B", "watchdog-connection-b"), now["value"])
    rt.reconcile_v1_private_read(adapter_a, expectation())
    rt.reconcile_v1_private_read(adapter_b, expectation())
    now["value"] = NOW + timedelta(seconds=16)
    rt.heartbeat()
    rt.stop()

    path = tmp_path / "runtime.db"
    b_sequences = set(_watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG-B", connection_id="watchdog-connection-b"
    ))
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path, sequences=b_sequences)

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    assert resumed.start().status == RuntimeStatus.RUNNING

    latest_a = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    latest_b = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG-B", connection_id="watchdog-connection-b"
    )
    assert latest_a is not None and latest_a.report["schema_version"].endswith("v1.2")
    assert latest_a.commissioning_hold
    assert latest_b is not None and latest_b.report["schema_version"].endswith("v1.2")
    assert latest_b.commissioning_hold
    assert latest_b.report["recovery_phase"] == ReconciliationWatchdogRecoveryPhase.REQUIRE_FRESH_RECONCILIATION.value


def test_mixed_version_interleaved_same_scope_downgrade_is_rejected_after_restart(tmp_path):
    now = {"value": NOW}
    rt, store, adapter_a = runtime(tmp_path, now)
    adapter_b = WatchdogAdapter(_scoped_capability("CRYPTO-WATCHDOG-B", "watchdog-connection-b"), now["value"])
    rt.reconcile_v1_private_read(adapter_a, expectation())
    rt.reconcile_v1_private_read(adapter_b, expectation())
    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    rt.stop()

    path = tmp_path / "runtime.db"
    a_sequences = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    b_sequences = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG-B", connection_id="watchdog-connection-b"
    )
    assert len(a_sequences) >= 3 and len(b_sequences) >= 3

    # A is made v1.0 -> v1.1 -> v1.0, with B records between those entries.
    _rewrite_watchdog_rows_as_legacy_v1_0(
        store, path, sequences={a_sequences[0], a_sequences[-1], *b_sequences}
    )

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    with pytest.raises(Exception, match="schema downgrade"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_mixed_version_legacy_label_on_current_payload_fails_closed_even_when_rehashed(tmp_path):
    now = {"value": NOW}
    rt, store, adapter_a = runtime(tmp_path, now)
    adapter_b = WatchdogAdapter(_scoped_capability("CRYPTO-WATCHDOG-B", "watchdog-connection-b"), now["value"])
    rt.reconcile_v1_private_read(adapter_a, expectation())
    rt.reconcile_v1_private_read(adapter_b, expectation())
    rt.stop()

    path = tmp_path / "runtime.db"
    b_sequence = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG-B", connection_id="watchdog-connection-b"
    )[0]
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT report_json FROM execution_v1_reconciliation_watchdog "
            "WHERE runtime_id='watchdog-runtime' AND sequence=?",
            (b_sequence,),
        ).fetchone()[0]
        payload = json.loads(raw)
        assert payload["schema_version"] == "EQS-EXEC-RECON-WATCHDOG-v1.2"
        payload["schema_version"] = "EQS-EXEC-RECON-WATCHDOG-v1.0"
        rewritten, db_hash = _rehash_watchdog_payload(payload)
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET report_json=?, report_sha256=? "
            "WHERE runtime_id='watchdog-runtime' AND sequence=?",
            (rewritten, db_hash, b_sequence),
        )

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    with pytest.raises(Exception, match="semantic assessment mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_mixed_version_current_label_on_legacy_payload_fails_closed_even_when_rehashed(tmp_path):
    now = {"value": NOW}
    rt, store, adapter_a = runtime(tmp_path, now)
    adapter_b = WatchdogAdapter(_scoped_capability("CRYPTO-WATCHDOG-B", "watchdog-connection-b"), now["value"])
    rt.reconcile_v1_private_read(adapter_a, expectation())
    rt.reconcile_v1_private_read(adapter_b, expectation())
    rt.stop()

    path = tmp_path / "runtime.db"
    b_sequence = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG-B", connection_id="watchdog-connection-b"
    )[0]
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path, sequences={b_sequence})

    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT report_json FROM execution_v1_reconciliation_watchdog "
            "WHERE runtime_id='watchdog-runtime' AND sequence=?",
            (b_sequence,),
        ).fetchone()[0]
        payload = json.loads(raw)
        assert payload["schema_version"] == "EQS-EXEC-RECON-WATCHDOG-v1.0"
        payload["schema_version"] = "EQS-EXEC-RECON-WATCHDOG-v1.2"
        rewritten, db_hash = _rehash_watchdog_payload(payload)
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET report_json=?, report_sha256=? "
            "WHERE runtime_id='watchdog-runtime' AND sequence=?",
            (rewritten, db_hash, b_sequence),
        )

    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )
    with pytest.raises(Exception, match="semantic assessment mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"

# Journal-structure replay tests exercise persistence corruption directly; all failures
# are discovered only after a new runtime instance replays the durable watchdog journal.
def _resume_watchdog_runtime(store: PersistentRuntimeStore, now) -> PersistentPaperShadowRuntime:
    return PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=store,
        paper_engine=paper_engine(), config=config(), clock=lambda: now["value"],
    )


def _swap_watchdog_sequences(path, first: int, second: int) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET sequence=-1 "
            "WHERE runtime_id='watchdog-runtime' AND sequence=?", (first,)
        )
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET sequence=? "
            "WHERE runtime_id='watchdog-runtime' AND sequence=?", (first, second)
        )
        connection.execute(
            "UPDATE execution_v1_reconciliation_watchdog SET sequence=? "
            "WHERE runtime_id='watchdog-runtime' AND sequence=-1", (second,)
        )
        connection.commit()
    _rebuild_watchdog_heads_for_fixture(path)


def test_mixed_version_replay_rejects_missing_watchdog_record_sequence_after_restart(tmp_path):
    now = {"value": NOW}
    rt, store, adapter_a = runtime(tmp_path, now)
    adapter_b = WatchdogAdapter(_scoped_capability("CRYPTO-WATCHDOG-B", "watchdog-connection-b"), now["value"])
    rt.reconcile_v1_private_read(adapter_a, expectation())
    rt.reconcile_v1_private_read(adapter_b, expectation())
    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    rt.stop()

    path = tmp_path / "runtime.db"
    b_sequences = set(_watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG-B", connection_id="watchdog-connection-b"
    ))
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path, sequences=b_sequences)

    with sqlite3.connect(path) as connection:
        middle = connection.execute(
            "SELECT sequence FROM execution_v1_reconciliation_watchdog "
            "WHERE runtime_id='watchdog-runtime' ORDER BY sequence LIMIT 1 OFFSET 1"
        ).fetchone()[0]
        connection.execute(
            "DELETE FROM execution_v1_reconciliation_watchdog "
            "WHERE runtime_id='watchdog-runtime' AND sequence=?", (middle,)
        )
        connection.commit()

    resumed = _resume_watchdog_runtime(store, now)
    with pytest.raises(Exception, match="sequence is not contiguous"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_mixed_version_replay_rejects_duplicated_legacy_logical_record_with_new_envelope_id(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path)

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT policy_id, venue_id, connection_id, assessed_at, last_reconciliation_id, next_due_at, "
            "status, commissioning_hold, report_json, report_sha256 "
            "FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        next_sequence = connection.execute(
            "SELECT MAX(sequence)+1 FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO execution_v1_reconciliation_watchdog(" 
            "runtime_id, sequence, assessment_id, policy_id, venue_id, connection_id, assessed_at, "
            "last_reconciliation_id, next_due_at, status, commissioning_hold, report_json, report_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("watchdog-runtime", next_sequence, "RCWD-DUPLICATE-LEGACY-ENVELOPE", *row),
        )
        connection.commit()

    resumed = _resume_watchdog_runtime(store, now)
    with pytest.raises(Exception, match="watchdog assessment identity mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_mixed_version_replay_rejects_duplicated_current_logical_record_with_new_envelope_id(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    rt.stop()
    path = tmp_path / "runtime.db"

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT policy_id, venue_id, connection_id, assessed_at, last_reconciliation_id, next_due_at, "
            "status, commissioning_hold, report_json, report_sha256 "
            "FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        next_sequence = connection.execute(
            "SELECT MAX(sequence)+1 FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO execution_v1_reconciliation_watchdog(" 
            "runtime_id, sequence, assessment_id, policy_id, venue_id, connection_id, assessed_at, "
            "last_reconciliation_id, next_due_at, status, commissioning_hold, report_json, report_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("watchdog-runtime", next_sequence, "RCWD-DUPLICATE-CURRENT-ENVELOPE", *row),
        )
        connection.commit()

    resumed = _resume_watchdog_runtime(store, now)
    with pytest.raises(Exception, match="watchdog assessment identity mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_mixed_version_replay_rejects_same_scope_out_of_order_assessments_after_restart(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    rt.stop()
    path = tmp_path / "runtime.db"

    sequences = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert len(sequences) >= 3
    _swap_watchdog_sequences(path, sequences[-2], sequences[-1])

    resumed = _resume_watchdog_runtime(store, now)
    with pytest.raises(Exception, match="watchdog assessment time is not monotonic|predecessor branch mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_mixed_version_replay_allows_global_reorder_when_each_scope_remains_ordered(tmp_path):
    now = {"value": NOW}
    rt, store, adapter_a = runtime(tmp_path, now)
    adapter_b = WatchdogAdapter(_scoped_capability("CRYPTO-WATCHDOG-B", "watchdog-connection-b"), now["value"])
    rt.reconcile_v1_private_read(adapter_a, expectation())
    rt.reconcile_v1_private_read(adapter_b, expectation())
    now["value"] += timedelta(seconds=1)
    rt.heartbeat()
    rt.stop()
    path = tmp_path / "runtime.db"

    a_sequences = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    b_sequences = _watchdog_sequences_for_scope(
        path, venue_id="CRYPTO-WATCHDOG-B", connection_id="watchdog-connection-b"
    )
    assert len(a_sequences) >= 2 and len(b_sequences) >= 2
    _rewrite_watchdog_rows_as_legacy_v1_0(store, path, sequences=set(b_sequences))

    # Swap A's second record with B's first. Each scope remains chronological,
    # but the global interleaving changes across schema versions.
    _swap_watchdog_sequences(path, a_sequences[1], b_sequences[0])

    resumed = _resume_watchdog_runtime(store, now)
    assert resumed.start().status == RuntimeStatus.RUNNING
    history = store.list_v1_reconciliation_watchdog("watchdog-runtime")
    assert len(history) >= 4
    for venue_id, connection_id in (
        ("CRYPTO-WATCHDOG", "watchdog-connection"),
        ("CRYPTO-WATCHDOG-B", "watchdog-connection-b"),
    ):
        scoped_times = [
            item.assessed_at for item in history
            if item.venue_id == venue_id and item.connection_id == connection_id
        ]
        assert scoped_times == sorted(scoped_times)

# Fork-replay tests validate explicit v1.2 predecessor linkage independently of the
# normal append path by writing structurally valid candidate branches directly.
def _insert_watchdog_payload(path, *, sequence: int, payload: dict) -> None:
    rewritten = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    db_hash = hashlib.sha256(rewritten.encode()).hexdigest()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO execution_v1_reconciliation_watchdog("
            "runtime_id, sequence, assessment_id, policy_id, venue_id, connection_id, assessed_at, "
            "last_reconciliation_id, next_due_at, status, commissioning_hold, report_json, report_sha256) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "watchdog-runtime", sequence, payload["assessment_id"], payload["policy_id"],
                payload["venue_id"], payload["connection_id"], payload["assessed_at"],
                payload.get("last_reconciliation_id"), payload["next_due_at"], payload["status"],
                int(payload["commissioning_hold"]), rewritten, db_hash,
            ),
        )
        connection.commit()


def _fork_base(tmp_path, now):
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    base = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    recon = store.latest_v1_private_reconciliation(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert base is not None and recon is not None
    policy = _stored_watchdog_policy(store)
    rt.stop()
    return store, adapter, policy, base, recon


def test_mixed_version_v1_1_history_upgrades_to_v1_2_with_explicit_parent_link(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    _rewrite_watchdog_rows_as_v1_1(store, path)

    legacy = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert legacy is not None
    assert legacy.report["schema_version"] == HYSTERESIS_WATCHDOG_SCHEMA_VERSION

    now["value"] += timedelta(seconds=1)
    resumed = _resume_watchdog_runtime(store, now)
    assert resumed.start().status == RuntimeStatus.RUNNING
    latest = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert latest is not None
    assert latest.report["schema_version"] == CURRENT_WATCHDOG_SCHEMA_VERSION
    assert latest.report["predecessor_assessment_id"] == legacy.assessment_id


def test_fork_replay_rejects_divergent_v1_2_successors_from_same_parent(tmp_path):
    now = {"value": NOW}
    store, _, policy, base, recon = _fork_base(tmp_path, now)
    previous = recovery_context_from_payload(base.report)
    healthy = assess_reconciliation_watchdog(
        policy, assessed_at=NOW + timedelta(seconds=1),
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True, previous_assessment=previous,
    )
    breached = assess_reconciliation_watchdog(
        policy, assessed_at=NOW + timedelta(seconds=16),
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True, previous_assessment=previous,
    )
    assert healthy.predecessor_assessment_id == base.assessment_id
    assert breached.predecessor_assessment_id == base.assessment_id
    assert healthy.status != breached.status

    path = tmp_path / "runtime.db"
    _insert_watchdog_payload(path, sequence=2, payload=healthy.to_payload())
    _insert_watchdog_payload(path, sequence=3, payload=breached.to_payload())
    now["value"] = NOW + timedelta(seconds=16)
    resumed = _resume_watchdog_runtime(store, now)
    with pytest.raises(Exception, match="predecessor branch mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_fork_replay_rejects_conflicting_same_time_successors_with_different_reconciliation_heads(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    first_report = rt.reconcile_v1_private_read(adapter, expectation())
    base = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert base is not None
    now["value"] += timedelta(seconds=1)
    adapter.observed_at = now["value"]
    second_report = rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()

    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM execution_v1_reconciliation_watchdog WHERE runtime_id='watchdog-runtime' AND sequence>1"
        )
        connection.commit()

    first = store.get_v1_private_reconciliation(
        "watchdog-runtime", first_report.reconciliation.reconciliation_id
    )
    second = store.get_v1_private_reconciliation(
        "watchdog-runtime", second_report.reconciliation.reconciliation_id
    )
    assert first is not None and second is not None and first.reconciliation_id != second.reconciliation_id
    policy = _stored_watchdog_policy(store)
    previous = recovery_context_from_payload(base.report)
    fork_at = NOW + timedelta(seconds=2)
    branch_a = assess_reconciliation_watchdog(
        policy, assessed_at=fork_at, last_reconciliation_id=first.reconciliation_id,
        last_reconciliation_completed_at=first.completed_at, last_reconciliation_complete=True,
        previous_assessment=previous,
    )
    branch_b = assess_reconciliation_watchdog(
        policy, assessed_at=fork_at, last_reconciliation_id=second.reconciliation_id,
        last_reconciliation_completed_at=second.completed_at, last_reconciliation_complete=True,
        previous_assessment=previous,
    )
    assert branch_a.assessment_id != branch_b.assessment_id
    assert branch_a.predecessor_assessment_id == branch_b.predecessor_assessment_id == base.assessment_id
    _insert_watchdog_payload(path, sequence=2, payload=branch_a.to_payload())
    _insert_watchdog_payload(path, sequence=3, payload=branch_b.to_payload())

    now["value"] = fork_at
    resumed = _resume_watchdog_runtime(store, now)
    with pytest.raises(Exception, match="predecessor branch mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


@pytest.mark.parametrize("branch_order", ["HEALTHY_FIRST", "BREACH_FIRST"])
def test_same_scope_branch_selection_cannot_be_forced_by_sequence_order_across_restart(tmp_path, branch_order):
    now = {"value": NOW}
    store, _, policy, base, recon = _fork_base(tmp_path, now)
    previous = recovery_context_from_payload(base.report)
    healthy = assess_reconciliation_watchdog(
        policy, assessed_at=NOW + timedelta(seconds=1),
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True, previous_assessment=previous,
    )
    breached = assess_reconciliation_watchdog(
        policy, assessed_at=NOW + timedelta(seconds=16),
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True, previous_assessment=previous,
    )
    ordered = (healthy, breached) if branch_order == "HEALTHY_FIRST" else (breached, healthy)
    path = tmp_path / "runtime.db"
    _insert_watchdog_payload(path, sequence=2, payload=ordered[0].to_payload())
    _insert_watchdog_payload(path, sequence=3, payload=ordered[1].to_payload())
    now["value"] = NOW + timedelta(seconds=16)

    resumed = _resume_watchdog_runtime(store, now)
    with pytest.raises(Exception, match="predecessor branch mismatch|assessment time is not monotonic"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_mixed_version_v1_1_parent_rejects_divergent_v1_2_successors_after_restart(tmp_path):
    now = {"value": NOW}
    store, _, policy, _, recon = _fork_base(tmp_path, now)
    path = tmp_path / "runtime.db"
    _rewrite_watchdog_rows_as_v1_1(store, path)
    base = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert base is not None and base.report["schema_version"] == HYSTERESIS_WATCHDOG_SCHEMA_VERSION
    previous = recovery_context_from_payload(base.report)
    branch_a = assess_reconciliation_watchdog(
        policy, assessed_at=NOW + timedelta(seconds=1),
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True, previous_assessment=previous,
    )
    branch_b = assess_reconciliation_watchdog(
        policy, assessed_at=NOW + timedelta(seconds=16),
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True, previous_assessment=previous,
    )
    assert branch_a.predecessor_assessment_id == branch_b.predecessor_assessment_id == base.assessment_id
    _insert_watchdog_payload(path, sequence=2, payload=branch_a.to_payload())
    _insert_watchdog_payload(path, sequence=3, payload=branch_b.to_payload())
    now["value"] = NOW + timedelta(seconds=16)

    resumed = _resume_watchdog_runtime(store, now)
    with pytest.raises(Exception, match="predecessor branch mismatch"):
        resumed.start()
    assert store.get_runtime("watchdog-runtime").status == "HALTED"


def test_single_v1_2_branch_with_explicit_successor_chain_replays_cleanly_after_restart(tmp_path):
    now = {"value": NOW}
    store, _, policy, base, recon = _fork_base(tmp_path, now)
    previous = recovery_context_from_payload(base.report)
    first = assess_reconciliation_watchdog(
        policy, assessed_at=NOW + timedelta(seconds=1),
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True, previous_assessment=previous,
    )
    second = assess_reconciliation_watchdog(
        policy, assessed_at=NOW + timedelta(seconds=2),
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True,
        previous_assessment=recovery_context_from_payload(first.to_payload()),
    )
    assert first.predecessor_assessment_id == base.assessment_id
    assert second.predecessor_assessment_id == first.assessment_id
    path = tmp_path / "runtime.db"
    _insert_watchdog_payload(path, sequence=2, payload=first.to_payload())
    _insert_watchdog_payload(path, sequence=3, payload=second.to_payload())
    _rebuild_watchdog_heads_for_fixture(path)
    now["value"] = NOW + timedelta(seconds=2)

    resumed = _resume_watchdog_runtime(store, now)
    assert resumed.start().status == RuntimeStatus.RUNNING

# Head-fencing tests prove write-time single-branch authority independently of restart replay.
def _head_fence_fixture(tmp_path, now):
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    base = store.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    recon = store.latest_v1_private_reconciliation(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert base is not None and recon is not None
    policy = _stored_watchdog_policy(store)
    return rt, store, adapter, policy, base, recon


def _successor(policy, base, recon, at):
    return assess_reconciliation_watchdog(
        policy,
        assessed_at=at,
        last_reconciliation_id=recon.reconciliation_id,
        last_reconciliation_completed_at=recon.completed_at,
        last_reconciliation_complete=True,
        previous_assessment=recovery_context_from_payload(base.report),
    )


def test_watchdog_head_is_atomically_bound_to_current_lease_generation(tmp_path):
    now = {"value": NOW}
    rt, store, _, policy, base, recon = _head_fence_fixture(tmp_path, now)
    successor = _successor(policy, base, recon, NOW + timedelta(seconds=1))
    sequence = store.append_v1_reconciliation_watchdog(
        "watchdog-runtime", rt.owner_id, generation=rt.generation, assessment=successor.to_payload()
    )
    head = store.get_v1_reconciliation_watchdog_head(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert head is not None
    assert head.assessment_id == successor.assessment_id
    assert head.sequence == sequence
    assert head.writer_owner_id == rt.owner_id
    assert head.writer_generation == rt.generation
    assert head.updated_at == successor.assessed_at


def test_concurrent_same_parent_successors_allow_exactly_one_head_advance(tmp_path):
    now = {"value": NOW}
    rt, store, _, policy, base, recon = _head_fence_fixture(tmp_path, now)
    first = _successor(policy, base, recon, NOW + timedelta(seconds=1))
    second = _successor(policy, base, recon, NOW + timedelta(seconds=16))
    assert first.predecessor_assessment_id == second.predecessor_assessment_id == base.assessment_id
    store_b = PersistentRuntimeStore(tmp_path / "runtime.db")

    def write(target_store, assessment):
        try:
            seq = target_store.append_v1_reconciliation_watchdog(
                "watchdog-runtime", rt.owner_id, generation=rt.generation, assessment=assessment.to_payload()
            )
            return ("OK", assessment.assessment_id, seq)
        except WatchdogHeadConflictError as exc:
            return ("CONFLICT", assessment.assessment_id, str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda args: write(*args), [(store, first), (store_b, second)]))
    assert sorted(result[0] for result in results) == ["CONFLICT", "OK"]
    winner = next(result[1] for result in results if result[0] == "OK")
    history = store.list_v1_reconciliation_watchdog("watchdog-runtime")
    assert [item.assessment_id for item in history].count(first.assessment_id) + [item.assessment_id for item in history].count(second.assessment_id) == 1
    head = store.get_v1_reconciliation_watchdog_head(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert head is not None and head.assessment_id == winner


def test_stale_process_cannot_append_after_lease_owner_and_generation_rollover(tmp_path):
    now = {"value": NOW}
    rt, store, _, policy, base, recon = _head_fence_fixture(tmp_path, now)
    stale_owner = rt.owner_id
    stale_generation = rt.generation
    candidate = _successor(policy, base, recon, NOW + timedelta(seconds=31))
    new_owner = f"replacement-{uuid4()}"
    replacement = store.claim(
        "watchdog-runtime", RuntimeMode.PAPER.value, new_owner,
        now=NOW + timedelta(seconds=31), lease_seconds=config().lease_seconds,
    )
    assert replacement.generation > stale_generation
    with pytest.raises(RuntimeLeaseError, match="current runtime lease generation"):
        store.append_v1_reconciliation_watchdog(
            "watchdog-runtime", stale_owner, generation=stale_generation, assessment=candidate.to_payload()
        )
    head = store.get_v1_reconciliation_watchdog_head(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert head is not None and head.assessment_id == base.assessment_id


def test_same_owner_stale_generation_is_fenced_after_reclaim(tmp_path):
    now = {"value": NOW}
    rt, store, _, policy, base, recon = _head_fence_fixture(tmp_path, now)
    old_generation = rt.generation
    candidate = _successor(policy, base, recon, NOW + timedelta(seconds=31))
    reclaimed = store.claim(
        "watchdog-runtime", RuntimeMode.PAPER.value, rt.owner_id,
        now=NOW + timedelta(seconds=31), lease_seconds=config().lease_seconds,
    )
    assert reclaimed.generation == old_generation + 1
    with pytest.raises(RuntimeLeaseError, match="current runtime lease generation"):
        store.append_v1_reconciliation_watchdog(
            "watchdog-runtime", rt.owner_id, generation=old_generation, assessment=candidate.to_payload()
        )


def test_head_update_failure_rolls_back_journal_insert_atomically(tmp_path):
    now = {"value": NOW}
    rt, store, _, policy, base, recon = _head_fence_fixture(tmp_path, now)
    successor = _successor(policy, base, recon, NOW + timedelta(seconds=1))
    path = tmp_path / "runtime.db"
    before = len(store.list_v1_reconciliation_watchdog("watchdog-runtime"))
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER synthetic_watchdog_head_crash BEFORE UPDATE ON execution_v1_reconciliation_watchdog_heads
            BEGIN SELECT RAISE(ABORT, 'synthetic watchdog head crash'); END;"""
        )
        connection.commit()
    with pytest.raises(sqlite3.IntegrityError, match="synthetic watchdog head crash"):
        store.append_v1_reconciliation_watchdog(
            "watchdog-runtime", rt.owner_id, generation=rt.generation, assessment=successor.to_payload()
        )
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER synthetic_watchdog_head_crash")
        connection.commit()
    assert len(store.list_v1_reconciliation_watchdog("watchdog-runtime")) == before
    assert store.get_v1_reconciliation_watchdog("watchdog-runtime", successor.assessment_id) is None
    head = store.get_v1_reconciliation_watchdog_head(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert head is not None and head.assessment_id == base.assessment_id


def test_legacy_mixed_version_database_without_head_table_is_backfilled_on_open(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    _rewrite_watchdog_rows_as_v1_1(store, path)
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE execution_v1_reconciliation_watchdog_heads")
        connection.commit()
    reopened = PersistentRuntimeStore(path)
    latest = reopened.latest_v1_reconciliation_watchdog(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    head = reopened.get_v1_reconciliation_watchdog_head(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert latest is not None and head is not None
    assert head.assessment_id == latest.assessment_id and head.sequence == latest.sequence
    assert head.writer_owner_id == "MIGRATED" and head.writer_generation == 0
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=reopened,
        paper_engine=paper_engine(), config=config(), clock=lambda: NOW + timedelta(seconds=1),
    )
    assert resumed.start().status == RuntimeStatus.RUNNING


def test_existing_head_fenced_database_does_not_silently_repair_missing_head_on_reopen(tmp_path):
    now = {"value": NOW}
    rt, store, adapter = runtime(tmp_path, now)
    rt.reconcile_v1_private_read(adapter, expectation())
    rt.stop()
    path = tmp_path / "runtime.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "DELETE FROM execution_v1_reconciliation_watchdog_heads "
            "WHERE runtime_id='watchdog-runtime' AND venue_id='CRYPTO-WATCHDOG' AND connection_id='watchdog-connection'"
        )
        connection.commit()
    reopened = PersistentRuntimeStore(path)
    assert reopened.get_v1_reconciliation_watchdog_head(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    ) is None
    resumed = PersistentPaperShadowRuntime(
        runtime_id="watchdog-runtime", mode=RuntimeMode.PAPER, store=reopened,
        paper_engine=paper_engine(), config=config(), clock=lambda: NOW + timedelta(seconds=1),
    )
    with pytest.raises(Exception, match="head scope set mismatch"):
        resumed.start()
    assert reopened.get_runtime("watchdog-runtime").status == "HALTED"


def test_crashed_process_cannot_append_after_its_lease_expires_without_takeover(tmp_path):
    now = {"value": NOW}
    rt, store, _, policy, base, recon = _head_fence_fixture(tmp_path, now)
    candidate = _successor(policy, base, recon, NOW + timedelta(seconds=31))
    # The runtime is intentionally not stopped or renewed: this simulates a crashed process
    # retaining stale in-memory owner/generation state after its durable lease has expired.
    with pytest.raises(RuntimeLeaseError, match="expired runtime lease"):
        store.append_v1_reconciliation_watchdog(
            "watchdog-runtime", rt.owner_id, generation=rt.generation, assessment=candidate.to_payload()
        )
    head = store.get_v1_reconciliation_watchdog_head(
        "watchdog-runtime", venue_id="CRYPTO-WATCHDOG", connection_id="watchdog-connection"
    )
    assert head is not None and head.assessment_id == base.assessment_id
