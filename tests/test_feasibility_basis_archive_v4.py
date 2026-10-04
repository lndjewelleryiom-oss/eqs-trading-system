from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import zipfile

import pytest

from quant_system.research.feasibility_basis_archive_v4 import parse_basis_kline_archive, validate_basis_archive


def archive(month: str, *, signed: bool = False, negative_price: bool = False, full: bool = True) -> bytes:
    year, number = map(int, month.split("-"))
    start = datetime(year, number, 1, tzinfo=timezone.utc)
    if number == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, number + 1, 1, tzinfo=timezone.utc)
    count = int((end - start).total_seconds() // 300) if full else 1
    rows = ["open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore"]
    for i in range(count):
        ts = start + timedelta(minutes=5 * i)
        epoch = int(ts.timestamp() * 1000)
        if signed:
            open_, high, low, close = Decimal("-0.0003"), Decimal("-0.0002"), Decimal("-0.0004"), Decimal("-0.00025")
        elif negative_price:
            open_, high, low, close = Decimal("-100"), Decimal("-99"), Decimal("-101"), Decimal("-100")
        else:
            open_, high, low, close = Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100.5")
        rows.append(f"{epoch},{open_},{high},{low},{close},0,{epoch+299999},0,1,0,0,0")
    payload = ("\n".join(rows) + "\n").encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr(f"BTCUSDT-5m-{month}.csv", payload)
    return out.getvalue()


def test_positive_mark_price_month_is_complete_and_valid():
    raw = archive("2023-02")
    receipt, rows = validate_basis_archive(raw, scope_id="scope", series="MARK_PRICE_5M", month="2023-02", source_url="https://example.test")
    assert len(rows) == 28 * 24 * 12
    assert receipt.signed_series is False
    assert receipt.locked_oos_touched is False


def test_signed_premium_index_accepts_negative_values():
    raw = archive("2023-02", signed=True)
    rows = parse_basis_kline_archive(raw, series="PREMIUM_INDEX_5M", month="2023-02")
    assert rows[0].open < 0
    assert rows[0].high > rows[0].low


def test_negative_mark_price_is_rejected():
    raw = archive("2023-02", negative_price=True)
    with pytest.raises(ValueError, match="price series must be positive"):
        parse_basis_kline_archive(raw, series="MARK_PRICE_5M", month="2023-02")


def test_locked_oos_month_is_rejected_before_coverage_claim():
    raw = archive("2026-04", full=False)
    with pytest.raises(ValueError, match="LOCKED_OOS_ACCESS_FORBIDDEN"):
        parse_basis_kline_archive(raw, series="INDEX_PRICE_5M", month="2026-04")
