import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "artifacts" / "test-evidence" / "R1_2_SUSTAINED_SOAK_2026-09-22.json"


def load():
    return json.loads(EVIDENCE.read_text())


def test_sustained_evidence_is_real_read_only_and_retains_failed_harness_result():
    data = load()
    assert data["test_data"] is False
    assert data["read_only"] is True
    assert data["acceptance"]["r1_2_overall"] == "TESTING"
    assert data["acceptance"]["failure_injection_binance_current_run"] == "FAILED_HARNESS_DEFECT"


def test_persistent_raw_capture_is_complete_and_hash_verified():
    raw = load()["persistent_raw_capture"]
    assert raw["received_frames"] == raw["persisted_rows"] == raw["sha256_verified_rows"] == 1366
    assert raw["persisted_bytes"] > 0
    assert raw["persistence_failures"] == 0


def test_three_hour_backfill_has_all_180_closed_one_minute_buckets_per_venue():
    data = load()["backfill_3h_1m"]
    assert data["expected_buckets_per_venue"] == 180
    for venue in ("BINANCE_USDM", "BYBIT_LINEAR", "OKX_SWAP"):
        item = data[venue]
        assert item == {"observed": 180, "missing": 0, "duplicates": 0, "complete": True}


def test_hourly_live_soak_is_not_falsely_claimed():
    data = load()
    assert data["hourly_checkpoint_status"]["actual_hourly_wall_clock_checkpoints_completed"] == 0
    assert data["acceptance"]["hourly_live_checkpoints"] == "NOT_EXECUTED"
    assert data["acceptance"]["multi_hour_live_soak"] == "NOT_EXECUTED"
