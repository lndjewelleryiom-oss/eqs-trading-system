from __future__ import annotations

from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path

import pytest

from quant_system.data.crypto_perps.real_market_population import load_public_rest_capture
from quant_system.data.crypto_perps.research_datasets import (
    DeterministicCryptoPerpReplay,
    HistoricalPartitionStore,
    InstrumentUniverseHistory,
    PartitionIntegrityError,
    PointInTimeDatasetAssembler,
)

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "artifacts/real-market/r1_3_2026-09-22/public_capture_source_bundle.json"
RAW_INTEGRITY = ROOT / "artifacts/real-market/r1_3_2026-09-22/raw_integrity_acceptance.json"
DATASET = "crypto-perps-normalized-v1"


def test_real_market_bundle_is_genuine_hash_attested_and_not_fixture_classified():
    payload = json.loads(SOURCE.read_text())
    assert payload["classification"] == "REAL MARKET"
    assert payload["source_verification"] == {"all_15_raw_hashes_match": True, "count": 15}
    assert len(payload["receipts"]) == 15
    assert "TEST_DATA" not in SOURCE.read_text()


def test_real_market_population_normalizes_three_public_venues_with_raw_lineage():
    population = load_public_rest_capture(SOURCE)
    assert len(population.receipts) == 15
    assert len(population.definitions) == 3
    assert len(population.events) == 39
    assert {definition.venue for definition in population.definitions} == {"BINANCE_USDM", "BYBIT_LINEAR", "OKX_SWAP"}
    assert {event.meta.venue for event in population.events} == {"BINANCE_USDM", "BYBIT_LINEAR", "OKX_SWAP"}
    assert all(event.meta.raw_sha256 in population.raw_hashes for event in population.events)
    assert all(event.meta.event_time <= event.meta.published_at <= event.meta.available_at <= event.meta.received_at for event in population.events)
    assert population.clock_corrections
    assert all(c.effective_available_at >= c.published_at for c in population.clock_corrections)


def test_genuine_instrument_snapshots_are_pit_unknown_before_capture_and_active_after_capture():
    population = load_public_rest_capture(SOURCE)
    universe = InstrumentUniverseHistory(population.definitions)
    assert set(universe.active_instruments_as_of(population.decision_time)) == {definition.instrument_id for definition in population.definitions}
    for definition in population.definitions:
        assert definition.effective_from < definition.available_at
        assert universe.definition_as_of(definition.instrument_id, definition.available_at - timedelta(microseconds=1)) is None
        assert universe.definition_as_of(definition.instrument_id, definition.available_at) == definition


def test_real_market_partitions_manifests_and_replay_are_reproducible_under_reordering(tmp_path):
    population = load_public_rest_capture(SOURCE)
    universe = InstrumentUniverseHistory(population.definitions)
    one = HistoricalPartitionStore(tmp_path / "one")
    two = HistoricalPartitionStore(tmp_path / "two")
    first_parts = one.write_events(DATASET, population.events)
    second_parts = two.write_events(DATASET, reversed(population.events))
    assert first_parts == second_parts
    assert len(first_parts) == 9
    first = PointInTimeDatasetAssembler(one, universe).assemble(
        dataset_id=DATASET, partitions=first_parts, decision_time=population.decision_time, start_time=population.start_time
    )
    second = PointInTimeDatasetAssembler(two, universe).assemble(
        dataset_id=DATASET, partitions=reversed(second_parts), decision_time=population.decision_time, start_time=population.start_time
    )
    assert first.manifest.fingerprint() == second.manifest.fingerprint()
    assert first.manifest.to_record() == second.manifest.to_record()
    assert first.events == second.events
    assert tuple(DeterministicCryptoPerpReplay(first).iter_until(population.decision_time)) == first.events
    assert tuple(DeterministicCryptoPerpReplay(second).iter_until(population.decision_time)) == second.events


def test_real_market_partition_tampering_fails_closed(tmp_path):
    population = load_public_rest_capture(SOURCE)
    store = HistoricalPartitionStore(tmp_path)
    descriptor = store.write_events(DATASET, population.events)[0]
    path = tmp_path / descriptor.relative_path
    path.write_bytes(path.read_bytes() + b"{}\n")
    with pytest.raises(PartitionIntegrityError, match="hash verification"):
        store.read_partition(descriptor)


def test_remote_raw_integrity_probe_links_all_receipts_and_detects_copy_tampering():
    population = load_public_rest_capture(SOURCE)
    evidence = json.loads(RAW_INTEGRITY.read_text())
    assert evidence["classification"] == "REAL MARKET DERIVED ACCEPTANCE"
    assert evidence["originals_read_only"] is True
    assert evidence["all_original_hashes_match"] is True
    assert evidence["tamper_probe"]["copy_only"] is True
    assert evidence["tamper_probe"]["detected"] is True
    indexed = {(row["venue"], row["label"]): row for row in evidence["raw_checks"]}
    assert len(indexed) == 15
    for receipt in population.receipts:
        assert indexed[(receipt.venue, receipt.label)]["expected"] == receipt.sha256
        assert indexed[(receipt.venue, receipt.label)]["actual"] == receipt.sha256
        assert indexed[(receipt.venue, receipt.label)]["match"] is True


def test_source_bundle_has_stable_snapshot_fingerprint():
    assert sha256(SOURCE.read_bytes()).hexdigest() == "5c08b7bb196f4b82df7c113bf9ae61799e7e478a90b1c05aa3540f7accbb734d"
