from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
import random
import subprocess
import sys
from pathlib import Path

import pytest

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
from quant_system.data.crypto_perps.research_datasets import (
    AssembledResearchDataset,
    HistoricalPartitionStore,
    InstrumentUniverseHistory,
    PointInTimeDatasetAssembler,
)
from quant_system.features import CryptoPerpetualFeatureEngine, FeatureLeakageError

UTC = timezone.utc
T0 = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
DECISION = T0 + timedelta(minutes=5)
DATASET_ID = "crypto-perps-normalized-v1"
INSTRUMENT = "BTC-USDT-PERP:BINANCE_USDM"
VENUE = "BINANCE_USDM"


def _hash(label: str) -> str:
    return sha256(label.encode()).hexdigest()


def _meta(kind: EventKind, offset_s: int, sequence: str, *, available_delay_ms: int = 20) -> MarketDataMeta:
    event_time = T0 + timedelta(seconds=offset_s)
    published_at = event_time + timedelta(milliseconds=5)
    available_at = published_at + timedelta(milliseconds=available_delay_ms)
    return MarketDataMeta(
        venue=VENUE,
        instrument_id=INSTRUMENT,
        venue_symbol="BTCUSDT",
        kind=kind,
        event_time=event_time,
        published_at=published_at,
        available_at=available_at,
        received_at=available_at + timedelta(milliseconds=3),
        source_channel="integration-test",
        source_sequence=sequence,
        raw_sha256=_hash(f"{kind.value}:{sequence}:{available_delay_ms}"),
    )


def _definition(*, status: str = "TRADING", effective_s: int = -3600, available_s: int = -3500, label: str = "active") -> PerpetualInstrumentDefinition:
    available = T0 + timedelta(seconds=available_s)
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
        status=status,
        effective_from=T0 + timedelta(seconds=effective_s),
        published_at=available - timedelta(milliseconds=5),
        available_at=available,
        received_at=available + timedelta(milliseconds=5),
        raw_sha256=_hash(label),
    )


def _events() -> tuple[object, ...]:
    return (
        PerpetualStateEvent(_meta(EventKind.PERPETUAL_STATE, 10, "state-0"), mark_price=Decimal("100"), index_price=Decimal("99"), funding_rate=Decimal("0.0001"), open_interest=Decimal("1000"), open_interest_value=Decimal("100000")),
        PerpetualStateEvent(_meta(EventKind.PERPETUAL_STATE, 250, "state-1"), mark_price=Decimal("101.5"), index_price=Decimal("100"), funding_rate=Decimal("0.0002"), open_interest=Decimal("1050"), open_interest_value=Decimal("106575")),
        PerpetualStateEvent(_meta(EventKind.PERPETUAL_STATE, 290, "state-2"), mark_price=Decimal("102"), index_price=Decimal("100"), funding_rate=Decimal("0.0003"), open_interest=Decimal("1100"), open_interest_value=Decimal("112200")),
        BookUpdate(_meta(EventKind.BOOK_SNAPSHOT, 260, "book-10"), bids=(BookLevel(Decimal("100"), Decimal("5")), BookLevel(Decimal("99"), Decimal("3"))), asks=(BookLevel(Decimal("101"), Decimal("2")), BookLevel(Decimal("102"), Decimal("4"))), first_sequence=10, final_sequence=10),
        BookUpdate(_meta(EventKind.BOOK_DELTA, 280, "book-11"), bids=(BookLevel(Decimal("100"), Decimal("6")),), asks=(BookLevel(Decimal("101"), Decimal("3")),), first_sequence=11, final_sequence=11, previous_sequence=10),
        TradeEvent(_meta(EventKind.TRADE, 270, "trade-1"), "t1", Decimal("101.7"), Decimal("2"), "BUY"),
        TradeEvent(_meta(EventKind.TRADE, 285, "trade-2"), "t2", Decimal("101.9"), Decimal("1"), "SELL"),
        LiquidationEvent(_meta(EventKind.LIQUIDATION, 275, "liq-1"), "l1", LiquidatedSide.LONG, Decimal("98"), Decimal("1")),
        LiquidationEvent(_meta(EventKind.LIQUIDATION, 295, "liq-2"), "l2", LiquidatedSide.SHORT, Decimal("103"), Decimal("0.5")),
    )


def _build(root: Path, *, shuffled: bool = False, definitions: tuple[PerpetualInstrumentDefinition, ...] | None = None):
    events = list(_events())
    if shuffled:
        random.Random(20260922).shuffle(events)
    store = HistoricalPartitionStore(root / "partitions")
    partitions = store.write_events(DATASET_ID, events)
    if shuffled:
        partitions = tuple(reversed(partitions))
    universe = InstrumentUniverseHistory(definitions or (_definition(),))
    assembler = PointInTimeDatasetAssembler(store, universe)
    source = R13FeatureDatasetSource(assembler, dataset_id=DATASET_ID, partitions=partitions)
    batch = source.load_feature_batch(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION)
    return source, batch, CryptoPerpetualFeatureEngine().compute(batch), partitions, universe


def _snapshot(root: Path, *, shuffled: bool = False) -> dict[str, object]:
    _, batch, run, _, _ = _build(root, shuffled=shuffled)
    return {
        "batch": batch.fingerprint,
        "datasets": batch.dataset_fingerprints,
        "universe": batch.universe_version,
        "manifest": run.manifest.fingerprint,
        "records": [record.fingerprint for record in run.records],
    }


def test_r13_pit_assembly_maps_to_feature_input_batch_and_preserves_lineage(tmp_path):
    _, batch, run, partitions, universe = _build(tmp_path)
    assembled = PointInTimeDatasetAssembler(HistoricalPartitionStore(tmp_path / "partitions"), universe).assemble(
        dataset_id=DATASET_ID, partitions=partitions, decision_time=DECISION
    )
    assert batch.events == assembled.events
    assert batch.dataset_fingerprints == (assembled.manifest.fingerprint(),)
    assert batch.universe_version == assembled.manifest.universe_fingerprint == universe.fingerprint()
    assert run.manifest.input_dataset_fingerprints == batch.dataset_fingerprints
    assert run.manifest.universe_version == batch.universe_version
    assert run.manifest.input_batch_fingerprint == batch.fingerprint


def test_future_availability_is_excluded_by_r13_before_feature_generation(tmp_path):
    events = list(_events())
    future = TradeEvent(_meta(EventKind.TRADE, 299, "future", available_delay_ms=5_000), "future", Decimal("102"), Decimal("1"), "BUY")
    events.append(future)
    store = HistoricalPartitionStore(tmp_path / "partitions")
    partitions = store.write_events(DATASET_ID, events)
    source = R13FeatureDatasetSource(PointInTimeDatasetAssembler(store, InstrumentUniverseHistory((_definition(),))), dataset_id=DATASET_ID, partitions=partitions)
    batch = source.load_feature_batch(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION)
    assert future not in batch.events
    assert all(event.meta.available_at <= DECISION for event in batch.events)
    run = CryptoPerpetualFeatureEngine().compute(batch)
    assert all(record.source_max_available_at <= DECISION for record in run.records)


def test_malformed_adapter_output_with_future_event_fails_closed_as_feature_leakage(tmp_path):
    _, batch, _, partitions, universe = _build(tmp_path)
    base_assembler = PointInTimeDatasetAssembler(HistoricalPartitionStore(tmp_path / "partitions"), universe)
    assembled = base_assembler.assemble(dataset_id=DATASET_ID, partitions=partitions, decision_time=DECISION)
    future = TradeEvent(_meta(EventKind.TRADE, 299, "bad", available_delay_ms=5_000), "bad", Decimal("102"), Decimal("1"), "BUY")

    class MalformedAssembler:
        def assemble(self, **kwargs):
            return AssembledResearchDataset(events=assembled.events + (future,), manifest=assembled.manifest)

    source = R13FeatureDatasetSource(MalformedAssembler(), dataset_id=DATASET_ID, partitions=partitions)  # type: ignore[arg-type]
    with pytest.raises(FeatureLeakageError, match="unavailable at decision time"):
        source.load_feature_batch(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION)
    assert batch.events == assembled.events


def test_listing_delisting_and_revision_point_in_time_correctness(tmp_path):
    v1 = _definition(status="TRADING", effective_s=-100, available_s=-90, label="v1")
    v2_future_revision = replace(v1, tick_size=Decimal("0.01"), available_at=DECISION + timedelta(seconds=10), received_at=DECISION + timedelta(seconds=11), raw_sha256=_hash("v2"))
    delisted = _definition(status="DELISTED", effective_s=280, available_s=310, label="delisted")
    history = InstrumentUniverseHistory((delisted, v2_future_revision, v1))
    assert history.definition_as_of(INSTRUMENT, DECISION) == v1
    assert history.active_instruments_as_of(DECISION) == (INSTRUMENT,)
    assert history.definition_as_of(INSTRUMENT, DECISION + timedelta(seconds=20)) == delisted
    assert history.active_instruments_as_of(DECISION + timedelta(seconds=20)) == ()

    store = HistoricalPartitionStore(tmp_path / "partitions")
    partitions = store.write_events(DATASET_ID, _events())
    source = R13FeatureDatasetSource(PointInTimeDatasetAssembler(store, history), dataset_id=DATASET_ID, partitions=partitions)
    historical = source.load_feature_batch(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION)
    later = source.load_feature_batch(instrument_id=INSTRUMENT, venue=VENUE, decision_time=DECISION + timedelta(seconds=20))
    assert historical.events == later.events  # delisting/revision does not rewrite legitimately historical observations


def test_shuffled_partition_and_event_enumeration_is_reproducible(tmp_path):
    first = _snapshot(tmp_path / "a", shuffled=False)
    second = _snapshot(tmp_path / "b", shuffled=True)
    assert first == second


def test_identical_canonical_inputs_produce_identical_feature_manifest_and_record_fingerprints(tmp_path):
    first = _snapshot(tmp_path / "one")
    second = _snapshot(tmp_path / "two")
    assert first["manifest"] == second["manifest"]
    assert first["records"] == second["records"]
    assert first["batch"] == second["batch"]


def test_cross_process_reproducibility_under_different_pythonhashseed(tmp_path):
    outputs = []
    test_target = f"{Path(__file__).resolve()}::test__cross_process_capture_worker"
    for seed in ("1", "999"):
        out = tmp_path / f"seed-{seed}.json"
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        env["EQS_REPRO_CAPTURE"] = str(out)
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", test_target], cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(json.loads(out.read_text()))
    assert outputs[0] == outputs[1]


def test__cross_process_capture_worker(tmp_path):
    destination = os.environ.get("EQS_REPRO_CAPTURE")
    if destination is None:
        pytest.skip("cross-process worker only")
    Path(destination).write_text(json.dumps(_snapshot(tmp_path), sort_keys=True))
