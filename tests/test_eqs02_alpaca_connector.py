from __future__ import annotations

from datetime import datetime, timezone
import hashlib

import pytest

from quant_system.data.equities.connectors.alpaca import AlpacaIexLatestBarConnector
from quant_system.data.equities.connectors.base import (
    UnknownEquityInstrumentError,
    UnsupportedEquityMessageError,
)

NOW=datetime(2026,10,4,12,0,tzinfo=timezone.utc)
RAW=hashlib.sha256(b"alpaca-fixture").hexdigest()


def connector_without_http():
    c=object.__new__(AlpacaIexLatestBarConnector)
    c._instrument_map={"SPY":"US:SPY","AAPL":"US:AAPL"}
    return c


def payload():
    return {
        "bars":{
            "SPY":{"t":"2026-10-02T19:59:00Z","o":670.0,"h":671.0,"l":669.0,"c":670.5,"v":12345,"n":100},
            "AAPL":{"t":"2026-10-02T19:59:00Z","o":260.0,"h":261.0,"l":259.0,"c":260.5,"v":23456,"n":200},
        }
    }


def test_normalizes_latest_closed_iex_bars():
    c=connector_without_http()
    rows=c.normalize_latest_bars(payload(),available_at=NOW,received_at=NOW,raw_sha256=RAW)
    assert [x.meta.instrument_id for x in rows]==["US:AAPL","US:SPY"]
    assert all(x.meta.venue=="ALPACA_IEX" for x in rows)
    assert all(x.meta.raw_sha256==RAW for x in rows)
    assert all(x.bar_end<=NOW for x in rows)


def test_unknown_symbol_fails_closed():
    c=connector_without_http()
    p=payload()
    p["bars"]["MSFT"]=p["bars"].pop("SPY")
    with pytest.raises(UnknownEquityInstrumentError):
        c.normalize_latest_bars(p,available_at=NOW,received_at=NOW,raw_sha256=RAW)


def test_unclosed_bar_fails_closed():
    c=connector_without_http()
    p={"bars":{"SPY":{"t":"2026-10-04T11:59:30Z","o":1,"h":1,"l":1,"c":1,"v":1,"n":1}}}
    with pytest.raises(UnsupportedEquityMessageError,match="not closed"):
        c.normalize_latest_bars(p,available_at=NOW,received_at=NOW,raw_sha256=RAW)


def test_decode_requires_object_root():
    c=connector_without_http()
    with pytest.raises(UnsupportedEquityMessageError):
        c.decode_json(b"[]")
    assert c.decode_json(b'{"bars":{}}')=={"bars":{}}


def test_empty_payload_fails_closed():
    c=connector_without_http()
    with pytest.raises(UnsupportedEquityMessageError,match="missing bars"):
        c.normalize_latest_bars({},available_at=NOW,received_at=NOW,raw_sha256=RAW)


def test_normalizes_historical_bars_per_symbol_in_order():
    c=connector_without_http()
    p={
        "bars":{
            "SPY":[
                {"t":"2026-10-02T19:58:00Z","o":669.0,"h":670.0,"l":668.0,"c":669.5,"v":10000,"n":90},
                {"t":"2026-10-02T19:59:00Z","o":670.0,"h":671.0,"l":669.0,"c":670.5,"v":12345,"n":100},
            ],
            "AAPL":[
                {"t":"2026-10-02T19:58:00Z","o":259.0,"h":260.0,"l":258.0,"c":259.5,"v":20000,"n":190},
                {"t":"2026-10-02T19:59:00Z","o":260.0,"h":261.0,"l":259.0,"c":260.5,"v":23456,"n":200},
            ],
        }
    }
    rows=c.normalize_bars(p,available_at=NOW,received_at=NOW,raw_sha256=RAW)
    assert set(rows)=={"AAPL","SPY"}
    assert len(rows["SPY"])==2 and len(rows["AAPL"])==2
    assert rows["SPY"][0].bar_start < rows["SPY"][1].bar_start


def test_historical_bars_reject_non_increasing_time():
    c=connector_without_http()
    p={"bars":{"SPY":[
        {"t":"2026-10-02T19:59:00Z","o":1,"h":1,"l":1,"c":1,"v":1,"n":1},
        {"t":"2026-10-02T19:59:00Z","o":1,"h":1,"l":1,"c":1,"v":1,"n":2},
    ]}}
    with pytest.raises(UnsupportedEquityMessageError,match="strictly increasing"):
        c.normalize_bars(p,available_at=NOW,received_at=NOW,raw_sha256=RAW)
