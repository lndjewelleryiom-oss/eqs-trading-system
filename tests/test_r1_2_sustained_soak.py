from datetime import datetime, timedelta, timezone

import pytest

from quant_system.data.crypto_perps.soak import (
    DropAccounting,
    HourlyCheckpointSchedule,
    PersistentRawCapture,
    RawCaptureRecord,
    reconcile_fixed_interval_backfill,
)

BASE = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)


def test_persistent_raw_capture_round_trips_exact_bytes_and_survives_reopen(tmp_path):
    path = tmp_path / "capture.jsonl"
    record = RawCaptureRecord.from_bytes(
        venue="BINANCE_USDM", channel="aggTrade", payload=b'{"x":1}',
        available_at=BASE, received_at=BASE + timedelta(milliseconds=2),
    )
    PersistentRawCapture(path).append(record)
    reopened = PersistentRawCapture(path).records()
    assert len(reopened) == 1
    assert reopened[0].payload() == b'{"x":1}'
    assert reopened[0].raw_sha256 == record.raw_sha256


def test_raw_capture_detects_tampering(tmp_path):
    path = tmp_path / "capture.jsonl"
    record = RawCaptureRecord.from_bytes(
        venue="OKX_SWAP", channel="trades", payload=b"abc",
        available_at=BASE, received_at=BASE,
    )
    PersistentRawCapture(path).append(record)
    text = path.read_text().replace(record.raw_b64, "eHl6")
    path.write_text(text)
    with pytest.raises(RuntimeError, match="integrity"):
        PersistentRawCapture(path).records()[0].payload()


def test_hourly_health_checkpoints_are_exact_across_multi_hour_virtual_soak():
    counters = DropAccounting(socket_frames_received=100, raw_records_persisted=100)
    schedule = HourlyCheckpointSchedule(BASE)
    checkpoints = schedule.due(BASE + timedelta(hours=3, minutes=4), counters)
    assert [c.elapsed for c in checkpoints] == [timedelta(hours=1), timedelta(hours=2), timedelta(hours=3)]
    assert all(c.healthy for c in checkpoints)
    assert schedule.due(BASE + timedelta(hours=3, minutes=59), counters) == ()
    assert len(schedule.due(BASE + timedelta(hours=4), counters)) == 1


def test_checkpoint_turns_unhealthy_when_drop_accounting_detects_gap():
    counters = DropAccounting(socket_frames_received=10, raw_records_persisted=9, persistence_failures=1)
    counters.assert_consistent()
    checkpoint = HourlyCheckpointSchedule(BASE).due(BASE + timedelta(hours=1), counters)[0]
    assert checkpoint.healthy is False
    assert "raw-persistence-gap" in checkpoint.reasons
    assert checkpoint.counters["detected_drops"] == 1


def test_drop_accounting_tracks_failure_injection_reconnect_and_resync():
    counters = DropAccounting(
        socket_frames_received=200, raw_records_persisted=200,
        forced_disconnects=3, reconnects=3, resynchronizations=3,
    )
    counters.assert_consistent()
    assert counters.detected_drops == 0
    assert counters.forced_disconnects == counters.reconnects == counters.resynchronizations == 3


def test_drop_accounting_rejects_unexplained_raw_persistence_gap():
    counters = DropAccounting(socket_frames_received=5, raw_records_persisted=4)
    with pytest.raises(ValueError, match="explicitly accounted"):
        counters.assert_consistent()


def test_three_hour_one_minute_backfill_reconciliation_is_exact():
    interval = 60_000
    start = 1_000_000
    stamps = [start + i * interval for i in range(180)]
    coverage = reconcile_fixed_interval_backfill(
        stamps, start_ms=start, end_ms=start + 179 * interval, interval_ms=interval
    )
    assert coverage.complete
    assert coverage.expected_buckets == coverage.observed_buckets == 180


def test_backfill_reconciliation_exposes_missing_and_duplicate_buckets():
    interval = 60_000
    start = 0
    stamps = [0, interval, interval, 3 * interval]
    coverage = reconcile_fixed_interval_backfill(stamps, start_ms=start, end_ms=3 * interval, interval_ms=interval)
    assert not coverage.complete
    assert coverage.missing_buckets == (2 * interval,)
    assert coverage.duplicate_buckets == (interval,)
