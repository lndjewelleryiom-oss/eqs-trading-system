import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "artifacts" / "test-evidence" / "R1_2_LIVE_ACCEPTANCE_2026-09-22.json"


def load():
    return json.loads(EVIDENCE.read_text())


def test_live_evidence_is_real_read_only_and_short_soak_passed():
    data = load()
    assert data["test_data"] is False
    assert data["read_only"] is True
    assert data["short_soak_core_pass"] is True
    assert len(data["venues"]) == 3


def test_all_three_venues_pass_reconnect_required_channels_and_backfill_reconciliation():
    by_venue = {item["venue"]: item for item in load()["venues"]}
    assert set(by_venue) == {"BINANCE_USDM", "BYBIT_LINEAR", "OKX_SWAP"}
    for venue in by_venue.values():
        assert venue["reconnect_pass"] is True
        assert venue["required_channels_pass"] is True
        assert venue["backfill_reconcile_pass"] is True
        assert venue["trade_id_overlap_count"] > 0
        assert venue["trade_id_overlap_count"] <= venue["trade_ws_count"]
        assert venue["latency_adjusted"]["n"] > 0
        assert venue["ws_error"] is None


def test_live_order_books_recover_uncrossed_after_forced_reconnect():
    for venue in load()["venues"]:
        for key in ("book_initial", "book_reconnect"):
            book = venue[key]
            assert book["crossed_book"] is False
            if venue["venue"] == "BINANCE_USDM":
                assert book["bridge_found"] is True
                assert book["continuity"] is True
            elif venue["venue"] == "BYBIT_LINEAR":
                assert book["snapshot_seen"] is True
                assert book["monotonic"] is True
            else:
                assert book["snapshot_seen"] is True
                assert book["continuity"] is True


def test_r12_evidence_retains_long_soak_limitations():
    limitations = " ".join(load()["limitations"]).lower()
    assert "multi-hour/day" in limitations
    assert "sustained-throughput" in limitations


def test_reproducible_runner_contains_no_embedded_ephemeral_token_or_private_trading_route():
    source = (ROOT / "scripts" / "r1_2_live_acceptance_edge.ts").read_text()
    assert 'EQS_R12_EPHEMERAL_TOKEN' in source
    assert 'const TOKEN = Deno.env.get("EQS_R12_EPHEMERAL_TOKEN") ?? "";' in source
    assert 'const TOKEN = "' not in source
    assert '/private' not in source
    assert 'place order' not in source.lower()
    assert 'wss://fstream.binance.com/public/' in source
    assert 'wss://fstream.binance.com/market/' in source
    assert 'wss://stream.bybit.com/v5/public/linear' in source
    assert 'wss://ws.okx.com:8443/ws/v5/public' in source
