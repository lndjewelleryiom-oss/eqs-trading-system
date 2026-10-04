from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import inspect
import math
import os
import random
import subprocess
import sys

import pytest

from quant_system.data.crypto_perps.models import (
    BookLevel,
    BookUpdate,
    EventKind,
    LiquidatedSide,
    LiquidationEvent,
    MarketDataMeta,
    PerpetualStateEvent,
    TradeEvent,
)
from quant_system.features import (
    CryptoPerpetualFeatureEngine,
    FeatureEngineConfig,
    FeatureFamily,
    FeatureInputBatch,
    FeatureInputError,
    FeatureLeakageError,
)
import quant_system.features.engine as feature_engine_module


DECISION = datetime(2026, 9, 22, 12, 5, 0, tzinfo=timezone.utc)
INSTRUMENT = "BTC-USDT-PERP:TEST"
VENUE = "TEST"
DATASET = sha256(b"canonical-research-dataset-v1").hexdigest()
UNIVERSE = "crypto-perps-universe-v1"


def _meta(kind: EventKind, event_time: datetime, sequence: str) -> MarketDataMeta:
    published = event_time + timedelta(milliseconds=10)
    available = event_time + timedelta(milliseconds=20)
    received = event_time + timedelta(milliseconds=30)
    return MarketDataMeta(
        venue=VENUE,
        instrument_id=INSTRUMENT,
        venue_symbol="BTCUSDT",
        kind=kind,
        event_time=event_time,
        published_at=published,
        available_at=available,
        received_at=received,
        source_channel=kind.value.lower(),
        source_sequence=sequence,
        raw_sha256=sha256(f"raw:{kind}:{sequence}".encode()).hexdigest(),
    )


def _fixture_events() -> tuple[object, ...]:
    state0 = PerpetualStateEvent(
        _meta(EventKind.PERPETUAL_STATE, DECISION - timedelta(seconds=290), "state-0"),
        mark_price=Decimal("100"),
        index_price=Decimal("99"),
        funding_rate=Decimal("0.0001"),
        open_interest=Decimal("1000"),
        open_interest_value=Decimal("100000"),
    )
    state1 = PerpetualStateEvent(
        _meta(EventKind.PERPETUAL_STATE, DECISION - timedelta(seconds=50), "state-1"),
        mark_price=Decimal("101.5"),
        index_price=Decimal("100"),
        funding_rate=Decimal("0.0002"),
        open_interest=Decimal("1050"),
        open_interest_value=Decimal("106575"),
    )
    state2 = PerpetualStateEvent(
        _meta(EventKind.PERPETUAL_STATE, DECISION - timedelta(seconds=10), "state-2"),
        mark_price=Decimal("102"),
        index_price=Decimal("100"),
        funding_rate=Decimal("0.0003"),
        open_interest=Decimal("1100"),
        open_interest_value=Decimal("112200"),
    )
    snapshot = BookUpdate(
        _meta(EventKind.BOOK_SNAPSHOT, DECISION - timedelta(seconds=40), "book-10"),
        bids=(
            BookLevel(Decimal("100"), Decimal("5")),
            BookLevel(Decimal("99"), Decimal("2.997")),
            BookLevel(Decimal("98"), Decimal("0.001")),
            BookLevel(Decimal("97"), Decimal("0.001")),
            BookLevel(Decimal("96"), Decimal("0.001")),
        ),
        asks=(
            BookLevel(Decimal("101"), Decimal("2")),
            BookLevel(Decimal("102"), Decimal("3.997")),
            BookLevel(Decimal("103"), Decimal("0.001")),
            BookLevel(Decimal("104"), Decimal("0.001")),
            BookLevel(Decimal("105"), Decimal("0.001")),
        ),
        first_sequence=10,
        final_sequence=10,
    )
    delta = BookUpdate(
        _meta(EventKind.BOOK_DELTA, DECISION - timedelta(seconds=20), "book-11"),
        bids=(BookLevel(Decimal("100"), Decimal("6")),),
        asks=(BookLevel(Decimal("101"), Decimal("3")),),
        first_sequence=11,
        final_sequence=11,
        previous_sequence=10,
    )
    trade_buy = TradeEvent(
        _meta(EventKind.TRADE, DECISION - timedelta(seconds=30), "trade-1"),
        trade_id="t1",
        price=Decimal("101.7"),
        quantity=Decimal("2"),
        aggressor_side="BUY",
    )
    trade_sell = TradeEvent(
        _meta(EventKind.TRADE, DECISION - timedelta(seconds=15), "trade-2"),
        trade_id="t2",
        price=Decimal("101.9"),
        quantity=Decimal("1"),
        aggressor_side="SELL",
    )
    long_liq = LiquidationEvent(
        _meta(EventKind.LIQUIDATION, DECISION - timedelta(seconds=25), "liq-1"),
        liquidation_id="l1",
        liquidated_side=LiquidatedSide.LONG,
        price=Decimal("98"),
        quantity=Decimal("1"),
    )
    short_liq = LiquidationEvent(
        _meta(EventKind.LIQUIDATION, DECISION - timedelta(seconds=5), "liq-2"),
        liquidation_id="l2",
        liquidated_side=LiquidatedSide.SHORT,
        price=Decimal("103"),
        quantity=Decimal("0.5"),
    )
    return (state0, state1, state2, snapshot, delta, trade_buy, trade_sell, long_liq, short_liq)


def _batch(events: tuple[object, ...] | None = None, *, decision: datetime = DECISION) -> FeatureInputBatch:
    return FeatureInputBatch(
        instrument_id=INSTRUMENT,
        venue=VENUE,
        decision_time=decision,
        events=tuple(events or _fixture_events()),
        dataset_fingerprints=(DATASET,),
        universe_version=UNIVERSE,
    )


def _values(run) -> dict[str, object]:
    return {record.definition.name: record.value for record in run.records}


def test_registry_is_versioned_deterministic_and_covers_all_requested_feature_families():
    engine = CryptoPerpetualFeatureEngine()
    assert {definition.family for definition in engine.registry.definitions} == set(FeatureFamily)
    assert all(definition.version == "1.0.0" for definition in engine.registry.definitions)
    assert engine.registry.fingerprint == CryptoPerpetualFeatureEngine().registry.fingerprint
    assert len({definition.feature_id for definition in engine.registry.definitions}) == len(engine.registry.definitions)


def test_microstructure_features_reconstruct_book_and_trade_imbalance():
    values = _values(CryptoPerpetualFeatureEngine().compute(_batch()))
    assert values["micro.spread_bps"] == pytest.approx(1 / 100.5 * 10_000)
    assert values["micro.book_imbalance_l5"] == pytest.approx((9 - 7) / (9 + 7))
    expected_microprice = (101 * 6 + 100 * 3) / 9
    assert values["micro.microprice_deviation_bps"] == pytest.approx((expected_microprice - 100.5) / 100.5 * 10_000)
    assert values["micro.top_depth_notional"] == pytest.approx(1608.0)
    assert values["micro.trade_imbalance_60s"] == pytest.approx(1 / 3)


def test_one_level_book_emits_only_features_semantically_supported_by_bbo():
    quote = BookUpdate(
        _meta(EventKind.BOOK_SNAPSHOT, DECISION - timedelta(seconds=5), "quote-1"),
        bids=(BookLevel(Decimal("100"), Decimal("3")),),
        asks=(BookLevel(Decimal("101"), Decimal("6")),),
    )
    values = _values(CryptoPerpetualFeatureEngine().compute(_batch((quote,))))
    assert "micro.spread_bps" in values
    assert "micro.microprice_deviation_bps" in values
    assert "micro.book_imbalance_l5" not in values
    assert "micro.top_depth_notional" not in values


def test_funding_basis_and_open_interest_features_are_point_in_time_state_derivatives():
    values = _values(CryptoPerpetualFeatureEngine().compute(_batch()))
    assert values["funding.current_rate"] == pytest.approx(0.0003)
    assert values["funding.mean_rate_300s"] == pytest.approx(0.0002)
    assert values["funding.change_300s"] == pytest.approx(0.0002)
    assert values["basis.mark_index_bps"] == pytest.approx(200.0)
    initial_basis = (100 / 99 - 1) * 10_000
    assert values["basis.change_300s_bps"] == pytest.approx(200.0 - initial_basis)
    assert values["open_interest.current"] == pytest.approx(1100.0)
    assert values["open_interest.value_current"] == pytest.approx(112200.0)
    assert values["open_interest.change_300s_pct"] == pytest.approx(10.0)


def test_liquidation_features_preserve_side_and_notional_pressure():
    values = _values(CryptoPerpetualFeatureEngine().compute(_batch()))
    assert values["liquidation.long_notional_60s"] == pytest.approx(98.0)
    assert values["liquidation.short_notional_60s"] == pytest.approx(51.5)
    assert values["liquidation.notional_60s"] == pytest.approx(149.5)
    assert values["liquidation.imbalance_60s"] == pytest.approx((98.0 - 51.5) / 149.5)


def test_volatility_momentum_and_regime_are_deterministic_descriptive_features_not_alpha_selection():
    run = CryptoPerpetualFeatureEngine().compute(_batch())
    values = _values(run)
    prices = (100.0, 101.5, 102.0)
    returns = (math.log(prices[1] / prices[0]), math.log(prices[2] / prices[1]))
    expected_rv = math.sqrt(sum(value * value for value in returns)) * 10_000
    expected_long_return = math.log(102 / 100) * 10_000
    assert values["volatility.realized_300s_bps"] == pytest.approx(expected_rv)
    assert values["volatility.range_300s_bps"] == pytest.approx(200.0)
    assert values["momentum.return_300s_bps"] == pytest.approx(expected_long_return)
    assert values["momentum.return_60s_bps"] == pytest.approx(math.log(102 / 101.5) * 10_000)
    assert values["momentum.efficiency_300s"] == pytest.approx(1.0)
    assert values["regime.state"] in {
        "LIQUIDITY_STRESS", "HIGH_VOLATILITY", "TRENDING_UP", "TRENDING_DOWN", "LOW_VOLATILITY_RANGE", "BALANCED"
    }
    # The fixture book has an intentionally wide ~99.5 bps spread, so liquidity stress dominates.
    assert values["regime.state"] == "LIQUIDITY_STRESS"


def test_future_available_event_is_rejected_before_any_feature_can_be_computed():
    future_event_time = DECISION - timedelta(milliseconds=5)
    future_meta = MarketDataMeta(
        venue=VENUE,
        instrument_id=INSTRUMENT,
        venue_symbol="BTCUSDT",
        kind=EventKind.TRADE,
        event_time=future_event_time,
        published_at=DECISION - timedelta(milliseconds=4),
        available_at=DECISION + timedelta(milliseconds=1),
        received_at=DECISION + timedelta(milliseconds=2),
        source_channel="trade",
        source_sequence="future",
        raw_sha256=sha256(b"future").hexdigest(),
    )
    future_trade = TradeEvent(future_meta, "future", Decimal("102"), Decimal("1"), "BUY")
    with pytest.raises(FeatureLeakageError, match="unavailable at decision time"):
        _batch(_fixture_events() + (future_trade,))


def test_every_feature_records_max_source_availability_at_or_before_decision_time():
    run = CryptoPerpetualFeatureEngine().compute(_batch())
    assert run.records
    for record in run.records:
        assert record.source_max_available_at <= DECISION
        assert record.source_events
        assert record.input_dataset_fingerprints == (DATASET,)
        assert record.universe_version == UNIVERSE
        assert len(record.fingerprint) == 64


def test_shuffled_input_produces_identical_feature_records_and_run_manifest():
    events = list(_fixture_events())
    baseline = CryptoPerpetualFeatureEngine().compute(_batch(tuple(events)))
    random.Random(20260922).shuffle(events)
    shuffled = CryptoPerpetualFeatureEngine().compute(_batch(tuple(events)))
    assert [record.canonical_payload() for record in baseline.records] == [record.canonical_payload() for record in shuffled.records]
    assert [record.fingerprint for record in baseline.records] == [record.fingerprint for record in shuffled.records]
    assert baseline.manifest.canonical_payload() == shuffled.manifest.canonical_payload()
    assert baseline.manifest.fingerprint == shuffled.manifest.fingerprint


def test_input_availability_or_dataset_version_change_changes_reproducibility_manifest():
    baseline = CryptoPerpetualFeatureEngine().compute(_batch())
    events = list(_fixture_events())
    first = events[0]
    shifted_meta = MarketDataMeta(
        venue=first.meta.venue,
        instrument_id=first.meta.instrument_id,
        venue_symbol=first.meta.venue_symbol,
        kind=first.meta.kind,
        event_time=first.meta.event_time,
        published_at=first.meta.published_at,
        available_at=first.meta.available_at + timedelta(milliseconds=1),
        received_at=first.meta.received_at + timedelta(milliseconds=1),
        source_channel=first.meta.source_channel,
        source_sequence=first.meta.source_sequence,
        raw_sha256=first.meta.raw_sha256,
    )
    events[0] = PerpetualStateEvent(
        shifted_meta,
        mark_price=first.mark_price,
        index_price=first.index_price,
        funding_rate=first.funding_rate,
        next_funding_time=first.next_funding_time,
        open_interest=first.open_interest,
        open_interest_value=first.open_interest_value,
    )
    shifted = CryptoPerpetualFeatureEngine().compute(_batch(tuple(events)))
    assert baseline.manifest.input_batch_fingerprint != shifted.manifest.input_batch_fingerprint
    assert baseline.manifest.fingerprint != shifted.manifest.fingerprint

    other_dataset = sha256(b"canonical-research-dataset-v2").hexdigest()
    versioned_batch = FeatureInputBatch(
        instrument_id=INSTRUMENT,
        venue=VENUE,
        decision_time=DECISION,
        events=_fixture_events(),
        dataset_fingerprints=(other_dataset,),
        universe_version=UNIVERSE,
    )
    versioned = CryptoPerpetualFeatureEngine().compute(versioned_batch)
    assert baseline.manifest.fingerprint != versioned.manifest.fingerprint
    assert {record.fingerprint for record in baseline.records}.isdisjoint(
        {record.fingerprint for record in versioned.records}
    )


def test_feature_version_and_configuration_are_part_of_registry_and_output_identity():
    baseline_engine = CryptoPerpetualFeatureEngine()
    changed_engine = CryptoPerpetualFeatureEngine(FeatureEngineConfig(feature_version="1.1.0"))
    assert baseline_engine.registry.fingerprint != changed_engine.registry.fingerprint
    baseline = baseline_engine.compute(_batch())
    changed = changed_engine.compute(_batch())
    assert baseline.manifest.fingerprint != changed.manifest.fingerprint
    assert baseline.records[0].fingerprint != changed.records[0].fingerprint


def test_batch_rejects_mixed_instrument_or_noncanonical_dataset_lineage():
    events = list(_fixture_events())
    first = events[0]
    wrong = MarketDataMeta(
        venue=VENUE,
        instrument_id="ETH-USDT-PERP:TEST",
        venue_symbol="ETHUSDT",
        kind=first.meta.kind,
        event_time=first.meta.event_time,
        published_at=first.meta.published_at,
        available_at=first.meta.available_at,
        received_at=first.meta.received_at,
        source_channel=first.meta.source_channel,
        source_sequence=first.meta.source_sequence,
        raw_sha256=first.meta.raw_sha256,
    )
    events[0] = PerpetualStateEvent(wrong, mark_price=Decimal("100"))
    with pytest.raises(FeatureInputError, match="match batch"):
        _batch(tuple(events))
    with pytest.raises(FeatureInputError, match="SHA-256"):
        FeatureInputBatch(INSTRUMENT, VENUE, DECISION, _fixture_events(), ("not-a-hash",), UNIVERSE)


def test_crossed_reconstructed_book_fails_closed():
    snapshot = BookUpdate(
        _meta(EventKind.BOOK_SNAPSHOT, DECISION - timedelta(seconds=10), "crossed"),
        bids=(BookLevel(Decimal("101"), Decimal("1")),),
        asks=(BookLevel(Decimal("100"), Decimal("1")),),
        first_sequence=1,
        final_sequence=1,
    )
    with pytest.raises(FeatureInputError, match="crossed"):
        CryptoPerpetualFeatureEngine().compute(_batch((snapshot,)))


def test_feature_engine_has_no_alpha_execution_or_live_commissioning_dependency():
    source = inspect.getsource(feature_engine_module)
    forbidden = (
        "quant_system.evolution",
        "quant_system.execution",
        "quant_system.paper",
        "quant_system.shadow",
        "quant_system.commissioning",
    )
    assert not any(module in source for module in forbidden)


def test_book_sequence_gap_is_rejected_before_microstructure_features_are_emitted():
    snapshot = BookUpdate(
        _meta(EventKind.BOOK_SNAPSHOT, DECISION - timedelta(seconds=20), "seq-10"),
        bids=(BookLevel(Decimal("100"), Decimal("1")),),
        asks=(BookLevel(Decimal("101"), Decimal("1")),),
        first_sequence=10,
        final_sequence=10,
    )
    gap = BookUpdate(
        _meta(EventKind.BOOK_DELTA, DECISION - timedelta(seconds=10), "seq-12"),
        bids=(BookLevel(Decimal("100"), Decimal("2")),),
        asks=(),
        first_sequence=12,
        final_sequence=12,
        previous_sequence=11,
    )
    with pytest.raises(ValueError, match="previous-sequence mismatch"):
        CryptoPerpetualFeatureEngine().compute(_batch((snapshot, gap)))



def test_manifest_is_reproducible_across_separate_python_processes_and_hash_seeds(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        """
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from quant_system.data.crypto_perps.models import EventKind, MarketDataMeta, PerpetualStateEvent
from quant_system.features import CryptoPerpetualFeatureEngine, FeatureInputBatch
D=datetime(2026,9,22,12,5,tzinfo=timezone.utc)
def state(offset, seq, mark, index, funding, oi):
    t=D-timedelta(seconds=offset)
    m=MarketDataMeta(venue='TEST',instrument_id='BTC-USDT-PERP:TEST',venue_symbol='BTCUSDT',kind=EventKind.PERPETUAL_STATE,event_time=t,published_at=t+timedelta(milliseconds=1),available_at=t+timedelta(milliseconds=2),received_at=t+timedelta(milliseconds=3),source_channel='state',source_sequence=seq,raw_sha256=sha256(seq.encode()).hexdigest())
    return PerpetualStateEvent(m,mark_price=Decimal(mark),index_price=Decimal(index),funding_rate=Decimal(funding),open_interest=Decimal(oi))
events=(state(250,'a','100','99','0.0001','1000'),state(10,'b','102','100','0.0002','1100'))
b=FeatureInputBatch(instrument_id='BTC-USDT-PERP:TEST',venue='TEST',decision_time=D,events=events,dataset_fingerprints=(sha256(b'dataset').hexdigest(),),universe_version='u1')
r=CryptoPerpetualFeatureEngine().compute(b)
print(r.manifest.fingerprint)
print('|'.join(x.fingerprint for x in r.records))
""",
        encoding="utf-8",
    )
    env = os.environ.copy()
    src = str((__import__('pathlib').Path(__file__).resolve().parents[1] / 'src'))
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    outputs = []
    for seed in ("1", "987654"):
        env["PYTHONHASHSEED"] = seed
        result = subprocess.run([sys.executable, str(probe)], check=True, text=True, capture_output=True, env=env)
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]
