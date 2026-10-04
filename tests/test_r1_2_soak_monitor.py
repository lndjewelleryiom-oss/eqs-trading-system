from pathlib import Path
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "r1_2_soak_monitor.py"


def _run(soak: Path, now: str) -> dict:
    cp = subprocess.run(
        [sys.executable, str(SCRIPT), "--soak-dir", str(soak), "--now", now],
        cwd=ROOT, text=True, capture_output=True, check=True,
    )
    return json.loads(cp.stdout)


def test_soak_monitor_records_growth_and_hash_chain(tmp_path):
    soak = tmp_path / "soak"; soak.mkdir()
    (soak / "binance_soak_state.json").write_text(json.dumps({
        "started_at":"2026-10-05T00:00:00Z","cycle_count":1,"counters":{"socket_frames_received":2}
    }))
    (soak / "binance_raw.jsonl").write_bytes(b"a" * 100)
    first = _run(soak, "2026-10-05T00:05:00Z")
    assert first["raw_bytes"] == 100 and first["raw_growth_bytes"] == 100
    assert first["hourly_checkpoint_count"] == 0 and first["previous_hash"] is None
    (soak / "binance_raw.jsonl").write_bytes(b"a" * 250)
    (soak / "binance_hourly_checkpoints.jsonl").write_text("{}\n")
    second = _run(soak, "2026-10-05T00:10:00Z")
    assert second["raw_bytes"] == 250 and second["raw_growth_bytes"] == 150
    assert second["sample_seconds"] == 300.0 and second["raw_bytes_per_second"] == 0.5
    assert second["hourly_checkpoint_count"] == 1
    assert second["previous_hash"] == first["record_sha256"]
