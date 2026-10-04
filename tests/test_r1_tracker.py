import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_r1_tracker_separates_foundation_from_live_feed_soak():
    tracker = json.loads((ROOT / "tracker.json").read_text())
    r11 = next(component for component in tracker["components"] if component["id"] == "R1.1")
    r12 = next(component for component in tracker["components"] if component["id"] == "R1.2")
    assert r11["status"] == "PASSED"
    assert r12["status"] == "TESTING"
    assert any("fixture" in item.lower() for item in r11["evidence"])
    assert any("multi-hour" in item.lower() for item in r12["remaining"])
    assert any("Binance" in item for item in r12["evidence"])
    assert (ROOT / "artifacts" / "test-evidence" / "R1_2_LIVE_ACCEPTANCE_2026-09-22.json").exists()
    assert (ROOT / "docs" / "R1_1_CRYPTO_PERPETUAL_DATA_FOUNDATION.md").exists()


def test_r12_tracker_retains_sustained_evidence_and_wall_clock_blocker():
    tracker = json.loads((ROOT / "tracker.json").read_text())
    r12 = next(component for component in tracker["components"] if component["id"] == "R1.2")
    joined_evidence = " ".join(r12["evidence"]).lower()
    joined_remaining = " ".join(r12["remaining"]).lower()
    assert "1366/1366" in joined_evidence
    assert "three-hour" in joined_evidence
    assert "failed_harness_defect" in joined_evidence
    assert "multi-hour" in joined_remaining
    assert "hourly" in joined_remaining
    assert (ROOT / "artifacts" / "test-evidence" / "R1_2_SUSTAINED_SOAK_2026-09-22.json").exists()
    assert (ROOT / "docs" / "R1_2_SUSTAINED_SOAK.md").exists()
