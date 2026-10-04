from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

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
    HistoricalPartitionStore,
    InstrumentUniverseHistory,
    PointInTimeDatasetAssembler,
    ResearchManifestCatalog,
)
from quant_system.research.r13_evidence_pipeline import (
    FROZEN_VENUES,
    FROZEN_WINDOWS,
    REQUIRED_SERIES,
)
from quant_system.research.r13_manifest import canonical_json


UTC = timezone.utc
DECISION = datetime(2026, 9, 21, 23, 59, 59, tzinfo=UTC)
BASE_EVENT = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)
AVAILABLE_FROM = datetime(2023, 1, 1, 0, 0, 0, tzinfo=UTC)
EFFECTIVE_FROM = datetime(2022, 1, 1, 0, 0, 0, tzinfo=UTC)


def _sha_bytes(value: bytes) -> str:
    return sha256(value).hexdigest()


def _sha_text(value: str) -> str:
    return _sha_bytes(value.encode("utf-8"))


def _write_json(path: Path, value: object) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_json(value)
    path.write_bytes(data)
    return _sha_bytes(data)


def _descriptor(path: Path) -> dict[str, str]:
    return {"path": str(path), "sha256": _sha_bytes(path.read_bytes())}


def _meta(
    *,
    venue: str,
    instrument_id: str,
    venue_symbol: str,
    kind: EventKind,
    t: datetime,
    raw_sha: str,
    seq: str,
) -> MarketDataMeta:
    return MarketDataMeta(
        venue=venue,
        instrument_id=instrument_id,
        venue_symbol=venue_symbol,
        kind=kind,
        event_time=t,
        published_at=t + timedelta(seconds=1),
        available_at=t + timedelta(seconds=2),
        received_at=t + timedelta(seconds=3),
        source_channel="TEST-COMMISSIONING-ONLY",
        source_sequence=seq,
        raw_sha256=raw_sha,
    )
def create_full_pass_source_bundle(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    raw_root = root / "raw"
    raw_root.mkdir()
    partition_root = root / "partitions"
    store = HistoricalPartitionStore(partition_root)

    assets = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "LINK", "AVAX")
    venue_assets = {
        "BINANCE_USDM": assets[0:3],
        "BYBIT_LINEAR": assets[3:6],
        "OKX_SWAP": assets[6:9],
    }

    definitions = []
    events = []
    receipts = []
    instrument_ids = []
    counter = 0
    for venue in FROZEN_VENUES:
        for asset in venue_assets[venue]:
            counter += 1
            instrument_id = f"{asset}-USDT-PERP:{venue}"
            venue_symbol = f"{asset}USDT" if venue != "OKX_SWAP" else f"{asset}-USDT-SWAP"
            instrument_ids.append((instrument_id, venue))
            raw_name = f"{venue}_{asset}_source.json"
            raw_path = raw_root / raw_name
            raw_bytes = canonical_json({
                "venue": venue,
                "asset": asset,
                "classification": "TEST-COMMISSIONING-ONLY",
            })
            raw_path.write_bytes(raw_bytes)
            raw_sha = _sha_bytes(raw_bytes)
            receipts.append({
                "venue": venue,
                "label": f"{asset}_historical",
                "filename": raw_name,
                "available_at": "2023-01-01T00:00:00Z",
                "received_at": "2023-01-01T00:00:01Z",
                "sha256": raw_sha,
                "bytes": len(raw_bytes),
                "status": 200,
            })
            definitions.append(
                PerpetualInstrumentDefinition(
                    instrument_id=instrument_id,
                    venue=venue,
                    venue_symbol=venue_symbol,
                    base_asset=asset,
                    quote_asset="USDT",
                    settle_asset="USDT",
                    contract_style="LINEAR",
                    tick_size=Decimal("0.1"),
                    lot_size=Decimal("0.001"),
                    contract_value=None,
                    status="TRADING",
                    effective_from=EFFECTIVE_FROM,
                    published_at=AVAILABLE_FROM,
                    available_at=AVAILABLE_FROM,
                    received_at=AVAILABLE_FROM + timedelta(seconds=1),
                    raw_sha256=raw_sha,
                )
            )
            t = BASE_EVENT + timedelta(minutes=counter)
            events.extend([
                TradeEvent(
                    meta=_meta(
                        venue=venue,
                        instrument_id=instrument_id,
                        venue_symbol=venue_symbol,
                        kind=EventKind.TRADE,
                        t=t,
                        raw_sha=raw_sha,
                        seq=f"{counter}-t",
                    ),
                    trade_id=f"trade-{counter}",
                    price=Decimal("100"),
                    quantity=Decimal("1"),
                    aggressor_side="BUY",
                ),
                BookUpdate(
                    meta=_meta(
                        venue=venue,
                        instrument_id=instrument_id,
                        venue_symbol=venue_symbol,
                        kind=EventKind.BOOK_SNAPSHOT,
                        t=t + timedelta(seconds=4),
                        raw_sha=raw_sha,
                        seq=f"{counter}-b",
                    ),
                    bids=(BookLevel(Decimal("99"), Decimal("2")),),
                    asks=(BookLevel(Decimal("101"), Decimal("2")),),
                ),
                PerpetualStateEvent(
                    meta=_meta(
                        venue=venue,
                        instrument_id=instrument_id,
                        venue_symbol=venue_symbol,
                        kind=EventKind.PERPETUAL_STATE,
                        t=t + timedelta(seconds=8),
                        raw_sha=raw_sha,
                        seq=f"{counter}-p",
                    ),
                    mark_price=Decimal("100"),
                    index_price=Decimal("100"),
                    funding_rate=Decimal("0.0001"),
                    next_funding_time=t + timedelta(hours=8),
                    open_interest=Decimal("1000"),
                    open_interest_value=Decimal("100000"),
                ),
                LiquidationEvent(
                    meta=_meta(
                        venue=venue,
                        instrument_id=instrument_id,
                        venue_symbol=venue_symbol,
                        kind=EventKind.LIQUIDATION,
                        t=t + timedelta(seconds=12),
                        raw_sha=raw_sha,
                        seq=f"{counter}-l",
                    ),
                    liquidation_id=f"liq-{counter}",
                    liquidated_side=LiquidatedSide.LONG,
                    price=Decimal("95"),
                    quantity=Decimal("0.5"),
                ),
            ])
    universe = InstrumentUniverseHistory(definitions)
    universe_path = root / "instrument_universe.json"
    universe.write(universe_path)

    descriptors = store.write_events("alpha-v1-r13", events)
    assembled = PointInTimeDatasetAssembler(store, universe).assemble(
        dataset_id="alpha-v1-r13",
        partitions=descriptors,
        decision_time=DECISION,
        start_time=datetime(2023, 1, 1, tzinfo=UTC),
    )
    catalog = ResearchManifestCatalog(root / "manifests")
    fingerprint = catalog.put(assembled.manifest)
    dataset_path = root / "manifests" / f"{fingerprint}.json"

    source_bundle_path = root / "source_bundle.json"
    _write_json(source_bundle_path, {
        "classification": "REAL MARKET",
        "remote_raw_root": str(raw_root),
        "source_verification": {
            "all_raw_hashes_match": True,
            "count": len(receipts),
        },
        "receipts": receipts,
    })

    coverage_records = []
    for window_name, (start, end) in FROZEN_WINDOWS.items():
        for instrument_id, venue in instrument_ids:
            for series in REQUIRED_SERIES:
                coverage_records.append({
                    "coverage_id": f"{window_name}:{venue}:{instrument_id}:{series}",
                    "window": window_name,
                    "venue": venue,
                    "instrument_id": instrument_id,
                    "series": series,
                    "start_time": start,
                    "end_time": end,
                    "expected_intervals": 1,
                    "observed_intervals": 1,
                    "missing_intervals": 0,
                    "partial_intervals": 0,
                    "availability_status": "COMPLETE",
                })
    coverage_path = root / "historical_coverage_evidence.json"
    _write_json(coverage_path, {
        "schema_id": "EQS-H02-HISTORICAL-COVERAGE-PROOF-v1",
        "source_class": "GENUINE",
        "status": "PASS",
        "windows": {key: list(value) for key, value in FROZEN_WINDOWS.items()},
        "breadth_fraction": 1.0,
        "records": coverage_records,
    })

    universe_proof_path = root / "historical_universe_evidence.json"
    _write_json(universe_proof_path, {
        "schema_id": "EQS-H02-HISTORICAL-UNIVERSE-PROOF-v1",
        "source_class": "GENUINE",
        "status": "PASS",
        "complete_listing_history": True,
        "complete_delisting_history": True,
        "complete_spec_revision_history": True,
        "complete_source_revision_history": True,
        "lifecycle_records": [],
    })

    liquidity_path = root / "liquidity_evidence.json"
    _write_json(liquidity_path, {
        "schema_id": "EQS-H02-LIQUIDITY-PROOF-v1",
        "source_class": "GENUINE",
        "status": "PASS",
        "window_days": 30,
        "pit_only": True,
        "instruments": [
            {
                "instrument_id": instrument_id,
                "window_days": 30,
                "pit_only": True,
                "rolling_30d_median_quote_volume_usd": 50_000_000,
            }
            for instrument_id, _ in instrument_ids
        ],
    })
    oos_path = root / "oos_access_log.json"
    _write_json(oos_path, {
        "schema_id": "EQS-OOS-ACCESS-SEAL-v1",
        "status": "SEALED",
        "outcome_viewed": False,
        "used_for_parameter_selection": False,
        "used_for_pruning": False,
        "used_for_ranking": False,
    })

    boundary_path = root / "protected_boundary_source.json"
    _write_json(boundary_path, {
        "schema_id": "EQS-PROTECTED-BOUNDARY-PROOF-v1",
        "r1_2_unchanged": True,
        "f7_unchanged": True,
        "tracker_unchanged": True,
        "broker_submission_enabled": False,
        "credentials_accessed": False,
        "live_capital_touched": False,
        "protected_files_compared": 35,
        "protected_files_changed": 0,
        "eqs06_options_included": False,
        "do_not_promote": True,
    })

    parent_path = root / "parent_archive.bin"
    parent_path.write_bytes(b"test-commissioning-parent-archive")

    feature_path = root / "feature_run_manifest.json"
    _write_json(feature_path, {
        "feature_engine_version": "crypto-perps-feature-engine-v1",
        "dataset_manifest_fingerprint": fingerprint,
        "universe_fingerprint": universe.fingerprint(),
        "max_decision_time": DECISION.isoformat(),
        "source_class": "TEST-COMMISSIONING-ONLY",
    })

    config = {
        "schema_id": "EQS-R1.3-HISTORICAL-EVIDENCE-PIPELINE-INPUT-v1",
        "run_id": "commissioning-full-pass",
        "manifest_id": "r13m_commissioning_full_pass",
        "manifest_generation": 1,
        "created_at": "2026-10-01T10:00:00Z",
        "dataset_manifest": _descriptor(dataset_path),
        "partition_root": str(partition_root),
        "universe_history": _descriptor(universe_path),
        "source_bundle": _descriptor(source_bundle_path),
        "parent_archive": _descriptor(parent_path),
        "historical_coverage_evidence": _descriptor(coverage_path),
        "historical_universe_evidence": _descriptor(universe_proof_path),
        "liquidity_evidence": _descriptor(liquidity_path),
        "oos_access_log": _descriptor(oos_path),
        "protected_boundary_source": _descriptor(boundary_path),
        "feature_run_manifests": [_descriptor(feature_path)],
        "code_version": "test-commissioning-v1",
        "dataset_builder_version": "r1.3-builder-v1",
        "build_command_fingerprint": _sha_text("commissioning-build-command"),
        "feature_definition_hash": _sha_text("commissioning-feature-definition"),
    }
    config_path = root / "pipeline_input.json"
    _write_json(config_path, config)
    return config_path
