from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest

from quant_system.research.feasibility_archive_v2 import (
    parse_funding_archive,
    parse_kline_archive,
    validate_archive,
    write_immutable_receipt,
)


def zipped(name: str, text: str) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(name, text)
    return stream.getvalue()


def millis(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def test_kline_archive_strictly_parses_contiguous_5m_rows() -> None:
    t0 = millis("2025-07-01T00:00:00Z")
    t1 = millis("2025-07-01T00:05:00Z")
    payload = zipped(
        "BTCUSDT-5m-2025-07.csv",
        "open_time,open,high,low,close,volume\n"
        f"{t0},100,102,99,101,10\n"
        f"{t1},101,103,100,102,11\n",
    )
    bars = parse_kline_archive(payload, month="2025-07")
    assert len(bars) == 2
    assert bars[0].close == 101
    assert (bars[1].open_time - bars[0].open_time).total_seconds() == 300


def test_kline_gap_fails_closed() -> None:
    t0 = millis("2025-07-01T00:00:00Z")
    t2 = millis("2025-07-01T00:10:00Z")
    payload = zipped("x.csv", f"{t0},100,101,99,100,1\n{t2},100,101,99,100,1\n")
    with pytest.raises(ValueError, match="non-5-minute gap"):
        parse_kline_archive(payload, month="2025-07")


def test_archive_rejects_locked_oos_month() -> None:
    t0 = millis("2026-04-01T00:00:00Z")
    payload = zipped("x.csv", f"{t0},100,101,99,100,1\n")
    with pytest.raises(ValueError, match="LOCKED_OOS"):
        parse_kline_archive(payload, month="2026-04")


def test_funding_header_and_hash_receipt(tmp_path: Path) -> None:
    t0 = millis("2025-07-01T00:00:00Z")
    t1 = millis("2025-07-01T08:00:00Z")
    payload = zipped(
        "BTCUSDT-fundingRate-2025-07.csv",
        "calc_time,funding_interval_hours,last_funding_rate\n"
        f"{t0},8,0.0001\n{t1},8,-0.0002\n",
    )
    rows = parse_funding_archive(payload, month="2025-07")
    assert len(rows) == 2
    receipt, normalized = validate_archive(
        payload,
        campaign_id="FEAS-BINANCE-BTC-MA-001",
        series="FUNDING_RATE",
        month="2025-07",
        source_url="https://example.invalid/archive.zip",
    )
    assert len(normalized) == 2
    assert receipt.locked_oos_touched is False
    assert len(receipt.archive_sha256) == 64
    path = tmp_path / "receipt.json"
    digest = write_immutable_receipt(path, receipt)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["receipt_sha256"] == digest
    assert document["archive_sha256"] == receipt.archive_sha256


def test_receipt_is_immutable(tmp_path: Path) -> None:
    t0 = millis("2025-07-01T00:00:00Z")
    payload = zipped("x.csv", f"{t0},8,0.0001\n")
    receipt, _ = validate_archive(
        payload,
        campaign_id="FEAS-BINANCE-BTC-MA-001",
        series="FUNDING_RATE",
        month="2025-07",
        source_url="https://example.invalid/a.zip",
    )
    path = tmp_path / "r.json"
    write_immutable_receipt(path, receipt)
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeError, match="immutable"):
        write_immutable_receipt(path, receipt)


def test_partial_month_cannot_receive_acquisition_receipt() -> None:
    t0 = millis("2025-07-01T00:00:00Z")
    payload = zipped("x.csv", f"{t0},100,101,99,100,1\n")
    with pytest.raises(ValueError, match="month coverage incomplete"):
        validate_archive(
            payload,
            campaign_id="FEAS-BINANCE-BTC-MA-001",
            series="KLINES_5M",
            month="2025-07",
            source_url="https://example.invalid/a.zip",
        )


def test_nested_zip_member_is_rejected() -> None:
    t0 = millis("2025-07-01T00:00:00Z")
    payload = zipped("nested/x.csv", f"{t0},100,101,99,100,1\n")
    with pytest.raises(ValueError, match="unsafe"):
        parse_kline_archive(payload, month="2025-07")
