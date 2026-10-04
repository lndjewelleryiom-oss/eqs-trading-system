import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_corrected_binance_live_reconnect_evidence_is_fail_closed_and_green():
    evidence = json.loads((ROOT / 'artifacts/test-evidence/R1_2_BINANCE_CORRECTED_RECONNECT_2026-09-22.json').read_text())
    assert evidence['test_data'] is False
    assert evidence['read_only'] is True
    assert evidence['pass'] is True
    assert evidence['counters']['socket_frames_received'] == evidence['counters']['raw_records_persisted'] == 71
    assert evidence['counters']['parse_failures'] == 0
    assert evidence['counters']['persistence_failures'] == 0
    assert evidence['counters']['sequence_gaps'] == 0
    assert evidence['reconnects'] == 1
    assert evidence['resynchronizations'] == 2
    assert len(evidence['cycles']) == 2
    for cycle in evidence['cycles']:
        assert cycle['snapshot_requested_while_socket_open'] is True
        assert cycle['bridge_found'] is True
        assert cycle['sequence_gaps'] == 0
        assert cycle['crossed_book'] is False
        assert cycle['ws_error'] is None
        assert cycle['snapshot_request_ms'] < cycle['snapshot_received_ms']


def test_real_hourly_checkpoint_gate_remains_open_until_wall_clock_hour_exists():
    status = json.loads((ROOT / 'artifacts/test-evidence/R1_2_BINANCE_HOURLY_MONITOR_STATUS_2026-09-22.json').read_text())
    assert status['test_data'] is False
    assert status['read_only'] is True
    assert status['real_hourly_checkpoints_completed'] == 0
    assert status['hourly_checkpoint_watch_configured'] is True
    assert status['r1_2_status'] == 'TESTING'


def test_reproducible_binance_soak_script_encodes_snapshot_while_buffering_before_close():
    script = (ROOT / 'scripts/r1_2_binance_sustained_soak.mjs').read_text()
    open_pos = script.index("new WebSocket('wss://fstream.binance.com/public/ws/btcusdt@depth@100ms')")
    snapshot_pos = script.index('const snapshot = await fetchSnapshot();')
    close_pos = script.index("ws.close(1000, 'forced failure injection')")
    assert open_pos < snapshot_pos < close_pos
    assert "nextCheckpoint = startedAt.getTime() + 3_600_000" in script
    assert 'detectedDrops = counters.parse_failures + counters.persistence_failures + counters.sequence_gaps' in script
