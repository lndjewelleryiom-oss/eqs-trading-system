from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

import pytest

from quant_system.data.equities.connectors.base import UnknownEquityInstrumentError, UnsupportedEquityMessageError
from quant_system.data.equities.connectors.yahoo_chart import YahooChartConnector


T = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
RAW = hashlib.sha256(b"fixture").hexdigest()


def payload():
    return {
        "chart": {
            "error": None,
            "result": [{
                "meta": {"symbol": "SPY", "dataGranularity": "1m"},
                "timestamp": [1791108000, 1791108060, 1791115200],
                "indicators": {"quote": [{
                    "open": [100.0, 101.0, 102.0],
                    "high": [101.0, 102.0, 103.0],
                    "low": [99.0, 100.0, 101.0],
                    "close": [100.5, 101.5, 102.5],
                    "volume": [1000, 1100, 1200],
                }]},
            }],
        }
    }


def test_normalizes_closed_bars_and_skips_incomplete_future_bar():
    c = YahooChartConnector({"SPY": "US:SPY"})
    events = c.normalize(payload(), available_at=T, received_at=T, raw_sha256=RAW)
    assert events
    assert all(e.meta.instrument_id == "US:SPY" for e in events)
    assert all(e.meta.raw_sha256 == RAW for e in events)
    assert all(e.bar_end <= T for e in events)


def test_unknown_symbol_fails_closed():
    c = YahooChartConnector({"AAPL": "US:AAPL"})
    with pytest.raises(UnknownEquityInstrumentError):
        c.normalize(payload(), available_at=T, received_at=T, raw_sha256=RAW)


def test_raw_sha_is_required():
    c = YahooChartConnector({"SPY": "US:SPY"})
    with pytest.raises(ValueError, match="raw_sha256"):
        c.normalize(payload(), available_at=T, received_at=T, raw_sha256=None)


def test_chart_error_fails_closed():
    c = YahooChartConnector({"SPY": "US:SPY"})
    p = payload()
    p["chart"]["error"] = {"code": "Bad Request"}
    with pytest.raises(UnsupportedEquityMessageError):
        c.normalize(p, available_at=T, received_at=T, raw_sha256=RAW)


def test_array_length_mismatch_fails_closed():
    c = YahooChartConnector({"SPY": "US:SPY"})
    p = payload()
    p["chart"]["result"][0]["indicators"]["quote"][0]["volume"] = [1]
    with pytest.raises(UnsupportedEquityMessageError, match="length mismatch"):
        c.normalize(p, available_at=T, received_at=T, raw_sha256=RAW)


def test_unsupported_interval_fails_closed():
    c = YahooChartConnector({"SPY": "US:SPY"})
    p = payload()
    p["chart"]["result"][0]["meta"]["dataGranularity"] = "3m"
    with pytest.raises(UnsupportedEquityMessageError, match="unsupported chart interval"):
        c.normalize(p, available_at=T, received_at=T, raw_sha256=RAW)
