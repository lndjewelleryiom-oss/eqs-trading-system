from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import RiskAction
from quant_system.data.crypto_perps.feature_source import R13FeatureDatasetSource
from quant_system.data.crypto_perps.models import (
    BookLevel,
    BookUpdate,
    EventKind,
    LiquidatedSide,
    LiquidationEvent,
    MarketDataMeta,
    PerpetualInstrumentDefinition,
    PerpetualStateEvent,
    TradeEvent,
)
from quant_system.data.crypto_perps.research_datasets import HistoricalPartitionStore, InstrumentUniverseHistory, PointInTimeDatasetAssembler
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter, VenueOrder, VenueOrderStatus
from quant_system.features import CryptoPerpetualFeatureEngine, FeatureInputBatch
from quant_system.monitoring import ExpectedBehavior, ObservedBehavior
from quant_system.nonlive import (
    DeterministicInfrastructureTestStrategy,
    DeterministicTestStrategyConfig,
    DuplicateMarketEventError,
    MarketDataSequenceError,
    NonLiveExecutionPipeline,
    SignalAction,
)
from quant_system.paper import PaperTradingEngine
from quant_system.risk.policy import PortfolioRiskSnapshot, RiskEngine, RiskLimits
from quant_system.runtime import DuplicateOrderError, PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeConfig, RuntimeLeaseError, RuntimeMode, RuntimeStatus
from quant_system.shadow import ShadowExecutionEngine

UTC = timezone.utc
T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
DECISION = T0 + timedelta(minutes=5)
DATASET_ID = "crypto-perps-normalized-v1"
INSTRUMENT = "BTC-USDT-PERP:BINANCE_USDM"
VENUE = "BINANCE_USDM"


def _hash(label: str) -> str:
    return sha256(label.encode()).hexdigest()


def _meta(kind: EventKind, offset_s: int, sequence: str, *, delay_ms: int = 20) -> MarketDataMeta:
    event_time = T0 + timedelta(seconds=offset_s)
    published_at = event_time + timedelta(milliseconds=5)
    available_at = published_at + timedelta(milliseconds=delay_ms)
    return MarketDataMeta(
        venue=VENUE,
        instrument_id=INSTRUMENT,
        venue_symbol="BTCUSDT",
        kind=kind,
        event_time=event_time,
        published_at=published_at,
        available_at=available_at,
        received_at=available_at + timedelta(milliseconds=2),
        source_channel="nonlive-acceptance",
        source_sequence=sequence,
        raw_sha256=_hash(f"{kind.value}:{sequence}:{delay_ms}"),
    )


def _definition() -> PerpetualInstrumentDefinition:
    available = T0 - timedelta(hours=1)
    return PerpetualInstrumentDefinition(
        instrument_id=INSTRUMENT,
        venue=VENUE,
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settle_asset="USDT",
        contract_style="LINEAR",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        contract_value=None,
        status="TRADING",
        effective_from=T0 - timedelta(hours=2),
        published_at=available - timedelta(milliseconds=5),
        available_at=available,
        received_at=available + timedelta(milliseconds=2),
        raw_sha256=_hash("instrument-v1"),
    )


def _events(*, sequence_fault: bool = False) -> tuple[object, ...]:
    previous = 9 if sequence_fault else 10
    return (
        PerpetualStateEvent(_meta(EventKind.PERPETUAL_STATE, 10, "state-0"), mark_price=Decimal("100"), index_price=Decimal("99"), funding_rate=Decimal("0.0001"), open_interest=Decimal("1000"), open_interest_value=Decimal("100000")),
        PerpetualStateEvent(_meta(EventKind.PERPETUAL_STATE, 250, "state-1"), mark_price=Decimal("101.5"), index_price=Decimal("100"), funding_rate=Decimal("0.0002"), open_interest=Decimal("1050"), open_interest_value=Decimal("106575")),
        PerpetualStateEvent(_meta(EventKind.PERPETUAL_STATE, 290, "state-2"), mark_price=Decimal("102"), index_price=Decimal("100"), funding_rate=Decimal("0.0003"), open_interest=Decimal("1100"), open_interest_value=Decimal("112200")),
        BookUpdate(_meta(EventKind.BOOK_SNAPSHOT, 260, "book-10"), bids=(BookLevel(Decimal("100"), Decimal("5")),), asks=(BookLevel(Decimal("101"), Decimal("3")),), first_sequence=10, final_sequence=10),
        BookUpdate(_meta(EventKind.BOOK_DELTA, 280, "book-11"), bids=(BookLevel(Decimal("100"), Decimal("6")),), asks=(BookLevel(Decimal("101"), Decimal("2")),), first_sequence=11, final_sequence=11, previous_sequence=previous),
        TradeEvent(_meta(EventKind.TRADE, 270, "trade-1"), "t1", Decimal("101.7"), Decimal("2"), "BUY"),
        TradeEvent(_meta(EventKind.TRADE, 285, "trade-2"), "t2", Decimal("101.9"), Decimal("1"), "SELL"),
        LiquidationEvent(_meta(EventKind.LIQUIDATION, 275, "liq-1"), "l1", LiquidatedSide.LONG, Decimal("98"), Decimal("1")),
        LiquidationEvent(_meta(EventKind.LIQUIDATION, 295, "liq-2"), "l2", LiquidatedSide.SHORT, Decimal("103"), Decimal("0.5")),
    )


def _source(root: Path, *, sequence_fault: bool = False) -> R13FeatureDatasetSource:
    store = HistoricalPartitionStore(root / "partitions")
    partitions = store.write_events(DATASET_ID, _events(sequence_fault=sequence_fault))
    assembler = PointInTimeDatasetAssembler(store, InstrumentUniverseHistory((_definition(),)))
    return R13FeatureDatasetSource(assembler, dataset_id=DATASET_ID, partitions=partitions)


def _assumptions() -> ExecutionAssumptions:
    return ExecutionAssumptions(
        commission_bps=Decimal("1"),
        spread_bps=Decimal("2"),
        slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"),
        financing_bps_annual=Decimal("0"),
        borrow_bps_annual=Decimal("0"),
        latency_ms=0,
    )


def _paper_engine() -> PaperTradingEngine:
    return PaperTradingEngine(ConservativeBarExecutionSimulator(_assumptions()), initial_cash=Decimal("10000"))


def _risk_engine(max_data_age: timedelta = timedelta(seconds=30)) -> RiskEngine:
    return RiskEngine(RiskLimits(
        max_order_notional=Decimal("1000000"),
        max_symbol_notional=Decimal("1000000"),
        max_strategy_notional=Decimal("1000000"),
        max_gross_notional=Decimal("1000000"),
        max_leverage=Decimal("100"),
        max_daily_loss=Decimal("5000"),
        max_drawdown_fraction=Decimal("0.50"),
        max_data_age=max_data_age,
    ))


def _risk_snapshot() -> PortfolioRiskSnapshot:
    return PortfolioRiskSnapshot(
        equity=Decimal("10000"),
        peak_equity=Decimal("10000"),
        gross_notional=Decimal("0"),
        symbol_notional={},
        strategy_notional={},
        daily_pnl=Decimal("0"),
        market_data_received_at=T0,
    )


def _bar(timestamp: datetime, *, volume: str = "1000") -> BarEvent:
    return BarEvent(INSTRUMENT, timestamp, Decimal("102"), Decimal("103"), Decimal("101"), Decimal("102"), Decimal(volume))


def _pipeline(source, runtime, *, quantity: str = "0.01", max_data_age: timedelta = timedelta(seconds=30)) -> NonLiveExecutionPipeline:
    return NonLiveExecutionPipeline(
        source=source,
        feature_engine=CryptoPerpetualFeatureEngine(),
        strategy=DeterministicInfrastructureTestStrategy(DeterministicTestStrategyConfig(order_quantity=Decimal(quantity))),
        risk_engine=_risk_engine(max_data_age),
        runtime=runtime,
    )


class MutableClock:
    def __init__(self, value: datetime):
        self.value = value

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs) -> None:
        self.value += timedelta(**kwargs)


def test_deterministic_feature_to_signal_risk_and_order_lineage(tmp_path):
    def capture(root: Path):
        store = PersistentRuntimeStore(root / "runtime.db")
        runtime = PersistentPaperShadowRuntime(runtime_id="deterministic", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine())
        runtime.start()
        result = _pipeline(_source(root), runtime).run_once(
            instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot()
        )
        return (
            result.batch.fingerprint,
            result.feature_run.manifest.fingerprint,
            result.strategy_decision.fingerprint,
            result.risk_decision.fingerprint,
            result.order.order_id,
        )

    first = capture(tmp_path / "a")
    second = capture(tmp_path / "b")
    assert first == second


def test_paper_path_accounting_and_end_to_end_lineage(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="paper-lineage", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine())
    runtime.start()
    result = _pipeline(_source(tmp_path), runtime).run_once(
        instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot()
    )
    assert result.strategy_decision.signal == SignalAction.BUY
    assert result.risk_decision.action == RiskAction.ALLOW
    assert result.order is not None
    assert runtime.paper_engine.simulator.pending_count == 1

    fills = runtime.on_bar(_bar(DECISION + timedelta(seconds=1)))
    assert len(fills) == 1
    snapshot = runtime.paper_engine.snapshot({INSTRUMENT: Decimal("102")})
    assert snapshot.positions[INSTRUMENT] == Decimal("0.01")
    assert runtime.paper_engine.ledger.reconcile().ok

    order_event = next(event for event in store.load_events("paper-lineage") if event.event_type == "ORDER_EVALUATED")
    lineage = order_event.payload["lineage"]
    assert lineage["dataset_fingerprints"] == list(result.batch.dataset_fingerprints)
    assert lineage["feature_manifest_fingerprint"] == result.feature_run.manifest.fingerprint
    assert lineage["strategy_decision_fingerprint"] == result.strategy_decision.fingerprint
    assert lineage["risk_decision_fingerprint"] == result.risk_decision.fingerprint
    assert lineage["order_fingerprint"] == result.risk_decision.order_fingerprint
    assert set(lineage["source_raw_sha256s"]) == set(result.strategy_decision.source_raw_sha256s)


def test_shadow_path_has_hard_zero_submit_and_persists_lineage(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=False)
    runtime = PersistentPaperShadowRuntime(runtime_id="shadow-lineage", mode=RuntimeMode.SHADOW, store=store, shadow_engine=ShadowExecutionEngine(gateway))
    runtime.start()
    result = _pipeline(_source(tmp_path), runtime).run_once(
        instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot()
    )
    assert result.risk_decision.action == RiskAction.ALLOW
    assert result.runtime_result.submission_result.sent is False
    assert result.runtime_result.submission_result.reason == "VENUE_SUBMISSION_DISABLED"
    assert adapter.submitted == []
    event = next(event for event in store.load_events("shadow-lineage") if event.event_type == "ORDER_EVALUATED")
    assert event.payload["path"] == "SHADOW_DISABLED_GATEWAY"
    assert event.payload["sent"] is False
    assert event.payload["lineage"]["order_fingerprint"] == result.risk_decision.order_fingerprint


def test_feed_staleness_becomes_explicit_risk_halt_without_order_submission(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="stale", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine())
    runtime.start()
    stale_decision = DECISION + timedelta(minutes=2)
    result = _pipeline(_source(tmp_path), runtime, max_data_age=timedelta(seconds=15)).run_once(
        instrument_id=INSTRUMENT, venue=VENUE, decision_time=stale_decision, risk_snapshot=_risk_snapshot()
    )
    assert result.risk_decision.action == RiskAction.HALT
    assert result.risk_decision.reason_codes == ("STALE_MARKET_DATA",)
    assert runtime.status == RuntimeStatus.HALTED
    assert runtime.paper_engine.simulator.pending_count == 0


def test_sequence_fault_is_contained_before_feature_or_order_path(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="sequence-fault", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine())
    runtime.start()
    with pytest.raises(MarketDataSequenceError):
        _pipeline(_source(tmp_path, sequence_fault=True), runtime).run_once(
            instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot()
        )
    assert runtime.status == RuntimeStatus.HALTED
    assert runtime.paper_engine.simulator.pending_count == 0
    assert store.event_count("sequence-fault", "MARKET_DATA_FAULT") == 1


def test_duplicate_market_event_is_contained_before_feature_generation(tmp_path):
    good_source = _source(tmp_path / "good")
    batch = good_source.load_feature_batch(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION)
    duplicate_batch = FeatureInputBatch(
        instrument_id=batch.instrument_id,
        venue=batch.venue,
        decision_time=batch.decision_time,
        events=batch.events + (batch.events[0],),
        dataset_fingerprints=batch.dataset_fingerprints,
        universe_version=batch.universe_version,
    )

    class DuplicateSource:
        def load_feature_batch(self, **kwargs):
            return duplicate_batch

    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="dup-event", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine())
    runtime.start()
    with pytest.raises(DuplicateMarketEventError):
        _pipeline(DuplicateSource(), runtime).run_once(
            instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot()
        )
    assert runtime.status == RuntimeStatus.HALTED
    assert runtime.paper_engine.simulator.pending_count == 0


@pytest.mark.parametrize("mode", [RuntimeMode.PAPER, RuntimeMode.SHADOW])
def test_duplicate_order_is_blocked_in_both_nonlive_modes(tmp_path, mode):
    store = PersistentRuntimeStore(tmp_path / f"{mode.value}.db")
    adapter = InMemoryBrokerAdapter()
    if mode == RuntimeMode.PAPER:
        runtime = PersistentPaperShadowRuntime(runtime_id=f"dup-{mode.value}", mode=mode, store=store, paper_engine=_paper_engine())
    else:
        runtime = PersistentPaperShadowRuntime(runtime_id=f"dup-{mode.value}", mode=mode, store=store, shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)))
    runtime.start()
    pipeline = _pipeline(_source(tmp_path / mode.value), runtime)
    first = pipeline.run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot())
    assert first.order is not None
    with pytest.raises(DuplicateOrderError):
        pipeline.run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot())
    assert store.event_count(f"dup-{mode.value}", "DUPLICATE_ORDER_BLOCKED") == 1
    if mode == RuntimeMode.SHADOW:
        assert adapter.submitted == []


def test_duplicate_bar_event_halts_paper_runtime_fail_closed(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="dup-bar", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine())
    runtime.start()
    _pipeline(_source(tmp_path), runtime).run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot())
    event = _bar(DECISION + timedelta(seconds=1))
    runtime.on_bar(event)
    with pytest.raises(ValueError, match="strictly increasing"):
        runtime.on_bar(event)
    assert runtime.status == RuntimeStatus.HALTED


def test_shadow_reconciliation_mismatch_halts_without_containment_submission(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shadow-reconcile",
        mode=RuntimeMode.SHADOW,
        store=store,
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
    )
    runtime.start()
    _pipeline(_source(tmp_path), runtime).run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot())
    unknown = uuid4()
    adapter.open_orders[unknown] = VenueOrder(unknown, "UNKNOWN", INSTRUMENT, VenueOrderStatus.ACKNOWLEDGED, Decimal("1"))
    result = runtime.reconcile_shadow(expected_open_order_ids=set(), expected_positions={})
    assert result.action.value == "HALT"
    assert runtime.status == RuntimeStatus.HALTED
    assert adapter.submitted == []
    assert adapter.canceled == []


def test_degradation_pause_halts_and_persists_after_integrated_decision(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="degrade", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine())
    runtime.start()
    _pipeline(_source(tmp_path), runtime).run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot())
    assessment = runtime.assess_degradation(
        ExpectedBehavior(0.55, 0.002, 1.5, 0.10, 5.0, 100),
        ObservedBehavior(0.30, -0.001, 0.2, 0.15, 12.0, 30),
    )
    assert assessment.state.value == "PAUSED"
    assert runtime.status == RuntimeStatus.HALTED
    assert store.get_runtime("degrade").status == "HALTED"


def test_unclean_restart_restores_pending_order_after_lease_expiry(tmp_path):
    clock = MutableClock(datetime(2026, 9, 22, 22, 0, tzinfo=UTC))
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    first = PersistentPaperShadowRuntime(runtime_id="crash-pending", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine(), clock=clock, config=RuntimeConfig(lease_seconds=5))
    first.start()
    _pipeline(_source(tmp_path), first).run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot())
    assert first.paper_engine.simulator.pending_count == 1
    # No stop/release: model a process crash, then let the durable lease expire.
    clock.advance(seconds=6)
    resumed = PersistentPaperShadowRuntime(runtime_id="crash-pending", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine(), clock=clock, config=RuntimeConfig(lease_seconds=5))
    resumed.start()
    assert resumed.paper_engine.simulator.pending_count == 1
    assert resumed.health().metrics["restarts"] == 1


def test_unclean_restart_restores_partial_fill_and_finishes_deterministically(tmp_path):
    clock = MutableClock(datetime(2026, 9, 22, 22, 30, tzinfo=UTC))
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    first = PersistentPaperShadowRuntime(runtime_id="crash-partial", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine(), clock=clock, config=RuntimeConfig(lease_seconds=5))
    first.start()
    _pipeline(_source(tmp_path), first, quantity="10").run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION, risk_snapshot=_risk_snapshot())
    first.on_bar(_bar(DECISION + timedelta(seconds=1), volume="100"))
    before = first.paper_engine.snapshot({INSTRUMENT: Decimal("102")})
    assert before.positions[INSTRUMENT] == Decimal("5")
    assert first.paper_engine.simulator.pending_count == 1
    clock.advance(seconds=6)

    resumed = PersistentPaperShadowRuntime(runtime_id="crash-partial", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine(), clock=clock, config=RuntimeConfig(lease_seconds=5))
    resumed.start()
    assert resumed.paper_engine.snapshot({INSTRUMENT: Decimal("102")}) == before
    assert resumed.paper_engine.simulator.pending_count == 1
    resumed.on_bar(_bar(DECISION + timedelta(seconds=2), volume="1000"))
    after = resumed.paper_engine.snapshot({INSTRUMENT: Decimal("102")})
    assert after.positions[INSTRUMENT] == Decimal("10")
    assert resumed.paper_engine.simulator.pending_count == 0
    assert resumed.paper_engine.ledger.reconcile().ok


def test_runtime_lease_collision_still_blocks_second_owner(tmp_path):
    clock = MutableClock(datetime(2026, 9, 22, 23, 0, tzinfo=UTC))
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    first = PersistentPaperShadowRuntime(runtime_id="lease", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine(), clock=clock)
    second = PersistentPaperShadowRuntime(runtime_id="lease", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine(), clock=clock)
    first.start()
    with pytest.raises(RuntimeLeaseError):
        second.start()


def test_sustained_fixture_replay_soak_keeps_accounting_and_event_chain_consistent(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    runtime = PersistentPaperShadowRuntime(runtime_id="fixture-soak", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine())
    runtime.start()
    pipeline = _pipeline(_source(tmp_path), runtime, max_data_age=timedelta(minutes=10))
    fingerprints: list[tuple[str, str, str]] = []
    for index in range(100):
        decision_time = DECISION + timedelta(seconds=index)
        result = pipeline.run_once(instrument_id=INSTRUMENT, venue=VENUE, decision_time=decision_time, risk_snapshot=_risk_snapshot())
        assert result.risk_decision.action == RiskAction.ALLOW
        assert result.order is not None
        fingerprints.append((result.feature_run.manifest.fingerprint, result.strategy_decision.fingerprint, result.risk_decision.fingerprint))
        runtime.on_bar(_bar(decision_time + timedelta(milliseconds=500), volume="1000"))
    assert len(set(fingerprints)) == 100
    assert runtime.paper_engine.simulator.pending_count == 0
    assert runtime.paper_engine.ledger.reconcile().ok
    assert runtime.paper_engine.snapshot({INSTRUMENT: Decimal("102")}).positions[INSTRUMENT] == Decimal("1.00")
    assert store.verify_events("fixture-soak") == len(store.load_events("fixture-soak"))
