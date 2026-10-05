from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from quant_system.research.historical_archive_v1 import (
    HistoricalPartitionDescriptor,
    HistoricalResearchArchiveManifest,
    certify_historical_archive,
)

UTC = timezone.utc
H = "a" * 64


def dt(y, m, d):
    return datetime(y, m, d, tzinfo=UTC)


def part(asset="FX", source="DUKASCOPY", pid="p1"):
    return HistoricalPartitionDescriptor(
        partition_id=pid, asset_class=asset, source_id=source, instrument_id="EURUSD" if asset == "FX" else "X",
        data_kind="BAR", period_start=dt(2023,1,1), period_end=dt(2026,4,1),
        earliest_available_at=dt(2023,1,2), latest_available_at=dt(2026,4,2), row_count=100,
        content_sha256=H, schema_version="v1", retrieval_sha256=H,
    )


def manifest(asset="FX"):
    source = "DUKASCOPY" if asset in {"FX","GOLD_SPOT_XAUUSD"} else ("ALPACA" if asset == "EQUITIES_ETFS" else "USTREASURY")
    p = part(asset=asset, source=source)
    return HistoricalResearchArchiveManifest(
        dataset_id="dataset-v1", asset_class=asset, source_id=source, format_version="v1",
        decision_time=dt(2026,4,5), historical_start=dt(2023,1,1), historical_end=dt(2026,4,1),
        partitions=(p,), universe_fingerprint=H,
        availability_policy="use only observations known by decision time",
        revision_policy="retain source revision lineage and never backfill revisions into earlier decisions",
        gap_policy="fail closed on unexplained gaps",
        corporate_action_policy="PIT splits/dividends/delistings" if asset == "EQUITIES_ETFS" else None,
        survivorship_safe_universe=True, historical_pit_authority=True, fabricated_volume=False,
    )


def splits():
    return {
        "training_start": dt(2023,1,1), "training_end": dt(2024,12,31),
        "validation_start": dt(2025,1,1), "validation_end": dt(2025,12,31),
        "oos_start": dt(2026,1,1), "oos_end": dt(2026,3,31),
    }


@pytest.mark.parametrize("asset", ["EQUITIES_ETFS", "FX", "GOLD_SPOT_XAUUSD", "RATES_FIXED_INCOME"])
def test_supported_archives_certify_training_validation_without_opening_oos(asset):
    result = certify_historical_archive(manifest(asset), split_spec=splits(), open_locked_oos=False)
    assert result.status == "PASS"
    assert result.training_authorised is True
    assert result.validation_authorised is True
    assert result.locked_oos_authorised is False
    assert result.broker_submission_enabled is False
    assert result.live_authority is False


def test_rates_fail_closed_when_historical_pit_authority_false():
    result = certify_historical_archive(replace(manifest("RATES_FIXED_INCOME"), historical_pit_authority=False), split_spec=splits())
    assert result.status == "BLOCKED"
    assert "HISTORICAL_PIT_AUTHORITY_FAILED" in result.blockers


def test_equities_require_corporate_action_policy():
    result = certify_historical_archive(replace(manifest("EQUITIES_ETFS"), corporate_action_policy=None), split_spec=splits())
    assert result.status == "BLOCKED"
    assert "CORPORATE_ACTION_POLICY_PRESENT_FAILED" in result.blockers


def test_oos_cannot_open_without_archive_coverage():
    m = replace(manifest("FX"), historical_end=dt(2026,1,15))
    result = certify_historical_archive(m, split_spec=splits(), open_locked_oos=True)
    assert result.status == "BLOCKED"
    assert result.locked_oos_authorised is False
    assert "LOCKED_OOS_COVERAGE_FAILED" in result.blockers


def test_manifest_rejects_trading_authority():
    with pytest.raises(ValueError, match="trading authority"):
        replace(manifest("FX"), live_authority=True)
