from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json

import pytest

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
    DeterministicCryptoPerpReplay,
    HistoricalPartitionStore,
    InstrumentUniverseHistory,
    PartitionIntegrityError,
    PointInTimeDatasetAssembler,
    ResearchManifestCatalog,
    event_from_record,
    event_to_record,
)

UTC = timezone.utc
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
DATASET = "crypto-perps-normalized-v1"
INSTRUMENT = "BTC-USDT-PERP:BINANCE_USDM"


def _hash(label: str) -> str:
    return sha256(label.encode()).hexdigest()


def _meta(
    *,
    kind: EventKind,
    offset_s: int,
    available_delay_ms: int = 20,
    instrument_id: str = INSTRUMENT,
    venue: str = "BINANCE_USDM",
    symbol: str = "BTCUSDT",
    sequence: str | None = None,
) -> MarketDataMeta:
    event_time = T0 + timedelta(seconds=offset_s)
    published_at = event_time + timedelta(milliseconds=5)
    available_at = published_at + timedelta(milliseconds=available_delay_ms)
    received_at = available_at + timedelta(milliseconds=3)
    return MarketDataMeta(
        venue=venue,
        instrument_id=instrument_id,
        venue_symbol=symbol,
        kind=kind,
        event_time=event_time,
        published_at=published_at,
        available_at=available_at,
        received_at=received_at,
        source_channel="test",
        source_sequence=sequence or str(offset_s),
        raw_sha256=_hash(f"{venue}:{instrument_id}:{kind}:{offset_s}:{available_delay_ms}"),
    )


def _trade(offset_s: int, *, instrument_id: str = INSTRUMENT, available_delay_ms: int = 20) -> TradeEvent:
    return TradeEvent(
        meta=_meta(
            kind=EventKind.TRADE,
            offset_s=offset_s,
            instrument_id=instrument_id,
            available_delay_ms=available_delay_ms,
        ),
        trade_id=f"t-{offset_s}",
        price=Decimal("65000.125") + Decimal(offset_s),
        quantity=Decimal("0.25"),
        aggressor_side="BUY" if offset_s % 2 == 0 else "SELL",
    )


def _book(offset_s: int) -> BookUpdate:
    return BookUpdate(
        meta=_meta(kind=EventKind.BOOK_DELTA, offset_s=offset_s),
        bids=(BookLevel(Decimal("64999.5"), Decimal("1.25")),),
        asks=(BookLevel(Decimal("65000.5"), Decimal("2.50")),),
        first_sequence=100 + offset_s,
        final_sequence=100 + offset_s,
        previous_sequence=99 + offset_s,
        checksum=42,
    )


def _state(offset_s: int) -> PerpetualStateEvent:
    return PerpetualStateEvent(
        meta=_meta(kind=EventKind.PERPETUAL_STATE, offset_s=offset_s),
        mark_price=Decimal("65000"),
        index_price=Decimal("64995"),
        funding_rate=Decimal("0.0001"),
        next_funding_time=T0 + timedelta(hours=8),
        open_interest=Decimal("10000"),
        open_interest_value=Decimal("650000000"),
    )


def _liq(offset_s: int) -> LiquidationEvent:
    return LiquidationEvent(
        meta=_meta(kind=EventKind.LIQUIDATION, offset_s=offset_s),
        liquidation_id=f"l-{offset_s}",
        liquidated_side=LiquidatedSide.LONG,
        price=Decimal("64000"),
        quantity=Decimal("3.5"),
    )


def _definition(
    *,
    status: str = "TRADING",
    effective_offset_s: int = -3600,
    available_offset_s: int = -3500,
    instrument_id: str = INSTRUMENT,
    raw_label: str = "definition",
) -> PerpetualInstrumentDefinition:
    available = T0 + timedelta(seconds=available_offset_s)
    return PerpetualInstrumentDefinition(
        instrument_id=instrument_id,
        venue="BINANCE_USDM",
        venue_symbol="BTCUSDT" if instrument_id == INSTRUMENT else "ETHUSDT",
        base_asset="BTC" if instrument_id == INSTRUMENT else "ETH",
        quote_asset="USDT",
        settle_asset="USDT",
        contract_style="LINEAR",
        tick_size=Decimal("0.1"),
        lot_size=Decimal("0.001"),
        contract_value=None,
        status=status,
        effective_from=T0 + timedelta(seconds=effective_offset_s),
        published_at=available - timedelta(milliseconds=5),
        available_at=available,
        received_at=available + timedelta(milliseconds=5),
        raw_sha256=_hash(raw_label),
    )


def _active_universe(*definitions: PerpetualInstrumentDefinition) -> InstrumentUniverseHistory:
    return InstrumentUniverseHistory(definitions or (_definition(),))


def test_canonical_event_codec_round_trips_all_crypto_perp_event_types():
    events = (_trade(1), _book(2), _state(3), _liq(4))
    round_tripped = tuple(event_from_record(json.loads(json.dumps(event_to_record(e)))) for e in events)
    assert round_tripped == events
    assert [e.meta.raw_sha256 for e in round_tripped] == [e.meta.raw_sha256 for e in events]


def test_partition_bytes_and_descriptor_are_deterministic_under_input_reordering(tmp_path):
    events = (_trade(3), _trade(1), _trade(2))
    first = HistoricalPartitionStore(tmp_path / "a").write_partition(DATASET, events)
    second = HistoricalPartitionStore(tmp_path / "b").write_partition(DATASET, tuple(reversed(events)))
    assert first == second
    assert (tmp_path / "a" / first.relative_path).read_bytes() == (tmp_path / "b" / second.relative_path).read_bytes()


def test_partition_writer_groups_by_venue_instrument_date_and_kind(tmp_path):
    later_day = replace(
        _trade(5),
        meta=replace(
            _trade(5).meta,
            event_time=T0 + timedelta(days=1, seconds=5),
            published_at=T0 + timedelta(days=1, seconds=5, milliseconds=5),
            available_at=T0 + timedelta(days=1, seconds=5, milliseconds=25),
            received_at=T0 + timedelta(days=1, seconds=5, milliseconds=28),
            raw_sha256=_hash("later-day"),
        ),
    )
    descriptors = HistoricalPartitionStore(tmp_path).write_events(DATASET, (_trade(1), _book(2), later_day))
    assert len(descriptors) == 3
    assert {d.key.kind for d in descriptors} == {EventKind.TRADE, EventKind.BOOK_DELTA}
    assert len({d.key.event_date for d in descriptors}) == 2


def test_partition_read_verifies_hash_row_count_key_schema_and_bounds(tmp_path):
    store = HistoricalPartitionStore(tmp_path)
    descriptor = store.write_partition(DATASET, (_trade(1), _trade(2)))
    path = tmp_path / descriptor.relative_path
    path.write_bytes(path.read_bytes() + b"{}\n")
    with pytest.raises(PartitionIntegrityError, match="hash verification"):
        store.read_partition(descriptor)


def test_universe_history_is_point_in_time_across_delisting_publication_delay():
    active = _definition(status="TRADING", available_offset_s=-100, raw_label="active")
    delisted = _definition(
        status="DELISTED",
        effective_offset_s=100,
        available_offset_s=200,
        raw_label="delisted",
    )
    history = InstrumentUniverseHistory((delisted, active))

    assert history.active_instruments_as_of(T0 + timedelta(seconds=150)) == (INSTRUMENT,)
    assert history.active_instruments_as_of(T0 + timedelta(seconds=250)) == ()
    assert history.definition_as_of(INSTRUMENT, T0 + timedelta(seconds=150)).status == "TRADING"
    assert history.definition_as_of(INSTRUMENT, T0 + timedelta(seconds=250)).status == "DELISTED"


def test_universe_history_fingerprint_and_persistence_are_deterministic(tmp_path):
    one = _definition(raw_label="one")
    two = _definition(
        instrument_id="ETH-USDT-PERP:BINANCE_USDM",
        raw_label="two",
        available_offset_s=-3400,
    )
    first = InstrumentUniverseHistory((one, two))
    second = InstrumentUniverseHistory((two, one))
    assert first.fingerprint() == second.fingerprint()

    path = tmp_path / "universe.json"
    assert first.write(path) == first.fingerprint()
    assert InstrumentUniverseHistory.read(path).fingerprint() == first.fingerprint()
    payload = json.loads(path.read_text())
    payload["definitions"][0]["status"] = "DELISTED"
    path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="integrity"):
        InstrumentUniverseHistory.read(path)


def test_pit_assembly_excludes_future_availability_but_preserves_historical_membership(tmp_path):
    active = _definition(status="TRADING", available_offset_s=-100, raw_label="active")
    delisted = _definition(
        status="DELISTED",
        effective_offset_s=100,
        available_offset_s=200,
        raw_label="delisted",
    )
    history = InstrumentUniverseHistory((active, delisted))
    store = HistoricalPartitionStore(tmp_path / "partitions")

    historical = _trade(150)
    future_meta = _meta(kind=EventKind.TRADE, offset_s=50, available_delay_ms=250_000)
    future = TradeEvent(future_meta, "future", Decimal("1"), Decimal("1"), "BUY")
    partitions = store.write_events(DATASET, (historical, future))

    assembled = PointInTimeDatasetAssembler(store, history).assemble(
        dataset_id=DATASET,
        partitions=partitions,
        decision_time=T0 + timedelta(seconds=250),
    )
    assert assembled.events == (historical,)
    # The instrument is delisted by final assembly time, but was legitimately active/known
    # when this event became available. Keeping it prevents survivorship bias.
    assert history.active_instruments_as_of(T0 + timedelta(seconds=250)) == ()


def test_pit_assembly_rejects_unknown_instrument_until_definition_is_available(tmp_path):
    eth_id = "ETH-USDT-PERP:BINANCE_USDM"
    eth = _definition(
        instrument_id=eth_id,
        effective_offset_s=-100,
        available_offset_s=100,
        raw_label="eth-listing",
    )
    event_before_listing_known = _trade(50, instrument_id=eth_id)
    store = HistoricalPartitionStore(tmp_path)
    partitions = store.write_events(DATASET, (event_before_listing_known,))
    assembled = PointInTimeDatasetAssembler(store, InstrumentUniverseHistory((eth,))).assemble(
        dataset_id=DATASET,
        partitions=partitions,
        decision_time=T0 + timedelta(seconds=200),
    )
    assert assembled.events == ()


def test_manifest_is_deterministic_across_partition_enumeration_and_repeated_assembly(tmp_path):
    history = _active_universe()
    store = HistoricalPartitionStore(tmp_path / "partitions")
    partitions = store.write_events(DATASET, (_trade(1), _book(2), _state(3), _liq(4)))
    assembler = PointInTimeDatasetAssembler(store, history)
    first = assembler.assemble(dataset_id=DATASET, partitions=partitions, decision_time=T0 + timedelta(minutes=1))
    second = assembler.assemble(
        dataset_id=DATASET,
        partitions=reversed(partitions),
        decision_time=T0 + timedelta(minutes=1),
    )
    assert first.events == second.events
    assert first.manifest.to_record() == second.manifest.to_record()
    assert first.manifest.fingerprint() == second.manifest.fingerprint()


def test_manifest_identity_changes_when_decision_boundary_changes(tmp_path):
    history = _active_universe()
    store = HistoricalPartitionStore(tmp_path)
    partitions = store.write_events(DATASET, (_trade(1), _trade(2)))
    assembler = PointInTimeDatasetAssembler(store, history)
    first = assembler.assemble(dataset_id=DATASET, partitions=partitions, decision_time=T0 + timedelta(seconds=10))
    second = assembler.assemble(dataset_id=DATASET, partitions=partitions, decision_time=T0 + timedelta(seconds=11))
    assert first.manifest.fingerprint() != second.manifest.fingerprint()


def test_manifest_catalog_detects_tampering(tmp_path):
    history = _active_universe()
    store = HistoricalPartitionStore(tmp_path / "partitions")
    partitions = store.write_events(DATASET, (_trade(1),))
    dataset = PointInTimeDatasetAssembler(store, history).assemble(
        dataset_id=DATASET,
        partitions=partitions,
        decision_time=T0 + timedelta(seconds=10),
    )
    catalog = ResearchManifestCatalog(tmp_path / "manifests")
    fingerprint = catalog.put(dataset.manifest)
    assert catalog.get(fingerprint) == dataset.manifest
    manifest_path = tmp_path / "manifests" / f"{fingerprint}.json"
    payload = json.loads(manifest_path.read_text())
    payload["event_count"] = 999
    manifest_path.write_text(json.dumps(payload))
    with pytest.raises(RuntimeError, match="integrity|mismatch"):
        catalog.get(fingerprint)


def test_deterministic_replay_is_stable_and_cannot_cross_manifest_decision_time(tmp_path):
    history = _active_universe()
    store = HistoricalPartitionStore(tmp_path)
    partitions = store.write_events(DATASET, (_trade(3), _state(2), _trade(1), _book(4)))
    dataset = PointInTimeDatasetAssembler(store, history).assemble(
        dataset_id=DATASET,
        partitions=partitions,
        decision_time=T0 + timedelta(seconds=10),
    )
    replay = DeterministicCryptoPerpReplay(dataset)
    early = tuple(replay.iter_until(T0 + timedelta(seconds=2, milliseconds=100)))
    assert [event.meta.event_time for event in early] == sorted(event.meta.event_time for event in early)
    assert all(event.meta.available_at <= T0 + timedelta(seconds=2, milliseconds=100) for event in early)
    assert tuple(replay.iter_until(T0 + timedelta(seconds=10))) == dataset.events
    with pytest.raises(ValueError, match="beyond"):
        tuple(replay.iter_until(T0 + timedelta(seconds=11)))


def test_replay_refuses_event_set_that_does_not_match_manifest(tmp_path):
    history = _active_universe()
    store = HistoricalPartitionStore(tmp_path)
    partitions = store.write_events(DATASET, (_trade(1), _trade(2)))
    dataset = PointInTimeDatasetAssembler(store, history).assemble(
        dataset_id=DATASET,
        partitions=partitions,
        decision_time=T0 + timedelta(seconds=10),
    )
    broken = replace(dataset, events=dataset.events[:-1])
    with pytest.raises(ValueError, match="manifest"):
        DeterministicCryptoPerpReplay(broken)


def test_partition_and_manifest_paths_are_idempotent_for_identical_inputs(tmp_path):
    store = HistoricalPartitionStore(tmp_path / "partitions")
    events = (_trade(1), _trade(2))
    first = store.write_partition(DATASET, events)
    second = store.write_partition(DATASET, events)
    assert first == second

    dataset = PointInTimeDatasetAssembler(store, _active_universe()).assemble(
        dataset_id=DATASET,
        partitions=(first,),
        decision_time=T0 + timedelta(seconds=10),
    )
    catalog = ResearchManifestCatalog(tmp_path / "catalog")
    assert catalog.put(dataset.manifest) == catalog.put(dataset.manifest)


def test_universe_membership_rejects_event_that_occurred_before_effective_listing_even_if_received_late():
    listing = _definition(
        status="TRADING",
        effective_offset_s=100,
        available_offset_s=90,
        raw_label="preannounced-listing",
    )
    history = InstrumentUniverseHistory((listing,))
    event = _trade(99, available_delay_ms=2_000)
    assert event.meta.available_at > listing.effective_from
    assert history.definition_as_of(INSTRUMENT, event.meta.available_at) == listing
    assert history.event_was_in_active_universe(event) is False


def test_partition_date_is_canonical_utc_date_not_source_timezone_date():
    # Same instant is 00:30 UTC but still prior calendar day in UTC-01:00.
    source_tz = timezone(timedelta(hours=-1))
    event_time = datetime(2026, 9, 19, 23, 30, tzinfo=source_tz)
    meta = MarketDataMeta(
        venue="BINANCE_USDM",
        instrument_id=INSTRUMENT,
        venue_symbol="BTCUSDT",
        kind=EventKind.TRADE,
        event_time=event_time,
        published_at=event_time + timedelta(milliseconds=5),
        available_at=event_time + timedelta(milliseconds=10),
        received_at=event_time + timedelta(milliseconds=15),
        source_channel="test",
        source_sequence="tz",
        raw_sha256=_hash("tz-event"),
    )
    event = TradeEvent(meta, "tz", Decimal("1"), Decimal("1"), "BUY")
    key = HistoricalPartitionStore.partition_key(DATASET, event)
    assert key.event_date.isoformat() == "2026-09-20"
