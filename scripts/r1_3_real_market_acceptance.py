from __future__ import annotations

import argparse
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
from time import perf_counter

from quant_system.data.crypto_perps.real_market_population import load_public_rest_capture
from quant_system.data.crypto_perps.research_datasets import (
    DeterministicCryptoPerpReplay,
    HistoricalPartitionStore,
    InstrumentUniverseHistory,
    PartitionIntegrityError,
    PointInTimeDatasetAssembler,
    ResearchManifestCatalog,
    event_to_record,
)

DATASET_ID = "crypto-perps-normalized-v1"


def replay_fingerprint(events) -> str:
    payload = [event_to_record(event) for event in events]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return sha256(raw).hexdigest()


def run(source_bundle: Path, raw_integrity: Path, output: Path) -> dict[str, object]:
    population = load_public_rest_capture(source_bundle)
    if output.exists():
        shutil.rmtree(output)
    partitions_root = output / "partitions"
    manifests_root = output / "manifests"
    output.mkdir(parents=True)

    universe = InstrumentUniverseHistory(population.definitions)
    universe_fingerprint = universe.write(output / "instrument_universe.json")
    store = HistoricalPartitionStore(partitions_root)
    started = perf_counter()
    descriptors = store.write_events(DATASET_ID, population.events)
    write_elapsed = perf_counter() - started
    partition_bytes = sum((partitions_root / descriptor.relative_path).stat().st_size for descriptor in descriptors)

    dataset = PointInTimeDatasetAssembler(store, universe).assemble(
        dataset_id=DATASET_ID,
        partitions=descriptors,
        decision_time=population.decision_time,
        start_time=population.start_time,
    )
    catalog = ResearchManifestCatalog(manifests_root)
    manifest_fingerprint = catalog.put(dataset.manifest)
    replay = DeterministicCryptoPerpReplay(dataset)
    replay_events = tuple(replay.iter_until(population.decision_time))
    replay_fp = replay_fingerprint(replay_events)

    with tempfile.TemporaryDirectory() as tmp:
        second_store = HistoricalPartitionStore(Path(tmp) / "partitions")
        second_descriptors = second_store.write_events(DATASET_ID, reversed(population.events))
        second_dataset = PointInTimeDatasetAssembler(second_store, universe).assemble(
            dataset_id=DATASET_ID,
            partitions=reversed(second_descriptors),
            decision_time=population.decision_time,
            start_time=population.start_time,
        )
        repeated_manifest_fp = second_dataset.manifest.fingerprint()
        repeated_replay_fp = replay_fingerprint(tuple(DeterministicCryptoPerpReplay(second_dataset).iter_until(population.decision_time)))

    with tempfile.TemporaryDirectory() as tmp:
        tamper_store = HistoricalPartitionStore(Path(tmp) / "partitions")
        tamper_descriptor = tamper_store.write_events(DATASET_ID, population.events)[0]
        tamper_path = tamper_store.root / tamper_descriptor.relative_path
        tamper_path.write_bytes(tamper_path.read_bytes() + b"{}\n")
        partition_tamper_detected = False
        try:
            tamper_store.read_partition(tamper_descriptor)
        except PartitionIntegrityError:
            partition_tamper_detected = True

    raw_integrity_payload = json.loads(raw_integrity.read_text(encoding="utf-8"))
    raw_hash_lookup = {(item["venue"], item["label"]): item for item in raw_integrity_payload["raw_checks"]}
    raw_receipts_match_remote = all(
        raw_hash_lookup[(receipt.venue, receipt.label)]["expected"] == receipt.sha256
        and raw_hash_lookup[(receipt.venue, receipt.label)]["match"] is True
        for receipt in population.receipts
    )

    iterations = 25
    perf_events = 0
    perf_bytes = 0
    started = perf_counter()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for index in range(iterations):
            perf_store = HistoricalPartitionStore(root / str(index))
            perf_descriptors = perf_store.write_events(DATASET_ID, population.events)
            perf_events += len(population.events)
            perf_bytes += sum((perf_store.root / descriptor.relative_path).stat().st_size for descriptor in perf_descriptors)
    throughput_elapsed = perf_counter() - started

    listing_checks = []
    active_now = universe.active_instruments_as_of(population.decision_time)
    for definition in population.definitions:
        before_known = universe.definition_as_of(definition.instrument_id, definition.available_at - timedelta(microseconds=1))
        listing_checks.append({
            "instrument_id": definition.instrument_id,
            "venue": definition.venue,
            "effective_from": definition.effective_from.isoformat(),
            "definition_available_at": definition.available_at.isoformat(),
            "known_immediately_before_capture": before_known is not None,
            "active_at_dataset_decision": definition.instrument_id in active_now,
        })

    report = {
        "classification": "REAL MARKET ACCEPTANCE",
        "dataset_id": DATASET_ID,
        "source_bundle": source_bundle.as_posix(),
        "source_bundle_sha256": population.source_bundle_sha256,
        "preserved_remote_raw_root": json.loads(source_bundle.read_text(encoding="utf-8"))["remote_raw_root"],
        "raw_capture_count": len(population.receipts),
        "raw_hashes_verified_on_capture_host": raw_integrity_payload.get("all_original_hashes_match") is True,
        "raw_receipts_match_remote_integrity_evidence": raw_receipts_match_remote,
        "raw_tamper_probe_detected": raw_integrity_payload.get("tamper_probe", {}).get("detected") is True,
        "normalized_event_count": len(population.events),
        "partition_count": len(descriptors),
        "partition_bytes": partition_bytes,
        "partition_write_seconds": write_elapsed,
        "single_build_events_per_second": len(population.events) / write_elapsed if write_elapsed else None,
        "single_build_bytes_per_second": partition_bytes / write_elapsed if write_elapsed else None,
        "throughput_iterations": iterations,
        "throughput_events": perf_events,
        "throughput_bytes": perf_bytes,
        "throughput_seconds": throughput_elapsed,
        "throughput_events_per_second": perf_events / throughput_elapsed if throughput_elapsed else None,
        "throughput_bytes_per_second": perf_bytes / throughput_elapsed if throughput_elapsed else None,
        "universe_fingerprint": universe_fingerprint,
        "active_instruments_at_decision": list(active_now),
        "listing_checks": listing_checks,
        "clock_correction_count": len(population.clock_corrections),
        "clock_corrections": [correction.to_record() for correction in population.clock_corrections],
        "manifest_fingerprint": manifest_fingerprint,
        "repeated_manifest_fingerprint": repeated_manifest_fp,
        "manifests_identical_across_repeated_builds": manifest_fingerprint == repeated_manifest_fp,
        "replay_fingerprint": replay_fp,
        "repeated_replay_fingerprint": repeated_replay_fp,
        "replays_identical_across_repeated_builds": replay_fp == repeated_replay_fp,
        "partition_tamper_detected": partition_tamper_detected,
        "raw_lineage_complete": all(event.meta.raw_sha256 in population.raw_hashes for event in dataset.events),
        "genuine_evidence_gaps": {
            "r1_2_raw_capture_or_backfill_payloads": "NOT PRESENT in supplied artifacts; R1.2 evidence summaries/checkpoints kept read-only and not substituted as raw data",
            "historical_delisting_capture": "NOT PRESENT for the three current BTC perpetual instruments",
            "historical_instrument_revision_capture": "NOT PRESENT for the three current BTC perpetual instruments",
            "fixture_substitution": "NONE; existing fixture tests remain TEST DATA and are not counted as REAL MARKET evidence",
        },
        "boundaries": {
            "f7_modified": False,
            "broker_submission_enabled": False,
            "credentials_accessed": False,
            "live_capital_touched": False,
            "alpha_selection_run": False,
        },
    }
    (output / "acceptance_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (output / "clock_corrections.json").write_text(
        json.dumps([correction.to_record() for correction in population.clock_corrections], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("artifacts/real-market/r1_3_2026-09-22/public_capture_source_bundle.json"))
    parser.add_argument("--raw-integrity", type=Path, default=Path("artifacts/real-market/r1_3_2026-09-22/raw_integrity_acceptance.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/real-market/r1_3_2026-09-22/materialized"))
    args = parser.parse_args()
    report = run(args.source, args.raw_integrity, args.output)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
