from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256

import pytest

from quant_system.data.equities.connectors.base import UnknownEquityInstrumentError
from quant_system.data.equities.connectors.kibot import KibotGuestDailySource
from quant_system.data.raw_store import ImmutableRawStore


AVAILABLE = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
RECEIVED = datetime(2026, 10, 4, 0, 0, 1, tzinfo=timezone.utc)
RAW = (
    b"09/30/2026,330.8,339.5,330.1401,333.01,39891309\r\n"
    b"10/01/2026,330.1,332.4816,325.81,330.48,27765618\r\n"
)


def parse(raw=RAW):
    return KibotGuestDailySource.parse(
        raw,
        symbol="AAPL",
        instrument_id="US:AAPL",
        raw_sha256=sha256(raw).hexdigest(),
        available_at=AVAILABLE,
        received_at=RECEIVED,
    )


def test_kibot_daily_parse_preserves_raw_lineage_and_prices():
    bars = parse()
    assert len(bars) == 2
    assert bars[0].meta.instrument_id == "US:AAPL"
    assert bars[0].meta.raw_sha256 == sha256(RAW).hexdigest()
    assert str(bars[0].open) == "330.8"
    assert str(bars[0].high) == "339.5"
    assert str(bars[0].low) == "330.1401"
    assert str(bars[0].close) == "333.01"
    assert str(bars[0].volume) == "39891309"
    assert bars[1].bar_start.date().isoformat() == "2026-10-01"


def test_kibot_daily_uses_conservative_local_availability_for_publication():
    bars = parse()
    assert all(bar.meta.published_at == AVAILABLE for bar in bars)
    assert all(bar.meta.available_at == AVAILABLE for bar in bars)
    assert all(bar.meta.received_at == RECEIVED for bar in bars)
    assert all(bar.bar_end <= bar.meta.published_at for bar in bars)


def test_kibot_daily_rejects_future_or_incomplete_session():
    raw = b"10/04/2026,100,101,99,100,1000\r\n"
    with pytest.raises(ValueError, match="INCOMPLETE_OR_FUTURE_SESSION"):
        KibotGuestDailySource.parse(
            raw,
            symbol="AAPL",
            instrument_id="US:AAPL",
            raw_sha256=sha256(raw).hexdigest(),
            available_at=AVAILABLE,
            received_at=RECEIVED,
        )


def test_kibot_daily_rejects_duplicate_sessions():
    raw = (
        b"10/01/2026,100,101,99,100,1000\r\n"
        b"10/01/2026,100,101,99,100,1000\r\n"
    )
    with pytest.raises(ValueError, match="DUPLICATE_SESSION_DATE"):
        KibotGuestDailySource.parse(
            raw,
            symbol="AAPL",
            instrument_id="US:AAPL",
            raw_sha256=sha256(raw).hexdigest(),
            available_at=AVAILABLE,
            received_at=RECEIVED,
        )


def test_kibot_daily_rejects_non_monotonic_sessions():
    raw = (
        b"10/02/2026,100,101,99,100,1000\r\n"
        b"10/01/2026,100,101,99,100,1000\r\n"
    )
    with pytest.raises(ValueError, match="NON_MONOTONIC_SESSION_ORDER"):
        KibotGuestDailySource.parse(
            raw,
            symbol="AAPL",
            instrument_id="US:AAPL",
            raw_sha256=sha256(raw).hexdigest(),
            available_at=AVAILABLE,
            received_at=RECEIVED,
        )


@pytest.mark.parametrize(
    "raw,reason",
    [
        (b"", "EMPTY_RESPONSE"),
        (b"10/01/2026,1,2,3\r\n", "ROW_FIELD_COUNT"),
        (b"bad-date,100,101,99,100,1000\r\n", "ROW_PARSE_FAILED"),
        (b"10/01/2026,100,99,98,100,1000\r\n", "bar high"),
    ],
)
def test_kibot_daily_rejects_malformed_payloads(raw, reason):
    with pytest.raises(ValueError, match=reason):
        KibotGuestDailySource.parse(
            raw,
            symbol="AAPL",
            instrument_id="US:AAPL",
            raw_sha256=sha256(raw).hexdigest(),
            available_at=AVAILABLE,
            received_at=RECEIVED,
        )


def test_kibot_symbol_map_fails_closed(tmp_path):
    source = KibotGuestDailySource({"AAPL": "US:AAPL"}, ImmutableRawStore(tmp_path / "raw"))
    assert source.instrument_id("aapl") == "US:AAPL"
    with pytest.raises(UnknownEquityInstrumentError):
        source.instrument_id("UNKNOWN")


def test_kibot_period_bounds_fail_before_network(tmp_path):
    source = KibotGuestDailySource({"AAPL": "US:AAPL"}, ImmutableRawStore(tmp_path / "raw"))
    with pytest.raises(ValueError, match="period"):
        source._download(None, "AAPL", 0)
    with pytest.raises(ValueError, match="period"):
        source._download(None, "AAPL", 1001)


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status
    def read(self):
        return self._payload
    def __enter__(self):
        return self
    def __exit__(self, exc_type, exc, tb):
        return False


class _FakeOpener:
    def __init__(self, payload: bytes, status: int = 200):
        self.payload = payload
        self.status = status
    def open(self, req, timeout=None):
        return _FakeResponse(self.payload, self.status)


@pytest.mark.parametrize(
    "payload",
    [
        b"200 OK\r\nSession inactivity timeout: 20 minutes\r\n",
        b"407 Already logged in for 0 minutes as 'guest'",
    ],
)
def test_kibot_guest_login_accepts_explicit_guest_success_states(tmp_path, payload):
    source = KibotGuestDailySource({"AAPL": "US:AAPL"}, ImmutableRawStore(tmp_path / "raw"))
    source._login(_FakeOpener(payload))


@pytest.mark.parametrize(
    "payload,status",
    [
        (b"407 Already logged in for 0 minutes as 'someone-else'", 200),
        (b"403 Forbidden", 200),
        (b"200 OK", 500),
    ],
)
def test_kibot_guest_login_rejects_non_guest_or_http_failure(tmp_path, payload, status):
    source = KibotGuestDailySource({"AAPL": "US:AAPL"}, ImmutableRawStore(tmp_path / "raw"))
    with pytest.raises(RuntimeError, match="KIBOT_GUEST_LOGIN_FAILED"):
        source._login(_FakeOpener(payload, status))
