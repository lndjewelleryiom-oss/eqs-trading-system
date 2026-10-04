from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from quant_system.data.equities.collector import ReadOnlyEquityMarketCollector
from quant_system.data.equities.connectors.base import (
    EquityInstrumentMappedConnector,
    UnknownEquityInstrumentError,
)
from quant_system.data.equities.models import (
    CorporateActionEvent,
    CorporateActionKind,
    EquityAssetType,
    EquityBarEvent,
    EquityEventKind,
    EquityInstrumentDefinition,
    EquityMarketDataMeta,
    EquityQuoteEvent,
    EquityTradeEvent,
)
from quant_system.data.raw_store import ImmutableRawStore


T0 = datetime(2026, 10, 4, 0, 0, tzinfo=timezone.utc)
RAW = "a" * 64


def meta(kind: EquityEventKind, *, raw_sha256: str = RAW, available_at: datetime | None = None):
    available = available_at or (T0 + timedelta(seconds=2))
    return EquityMarketDataMeta(
        venue="TEST_EXCHANGE",
        instrument_id="US:AAPL",
        venue_symbol="AAPL",
        kind=kind,
        event_time=T0,
        published_at=T0 + timedelta(seconds=1),
        available_at=available,
        received_at=available + timedelta(seconds=1),
        source_channel="fixture",
        source_sequence="1",
        raw_sha256=raw_sha256,
    )


def test_equity_meta_is_point_in_time_and_identity_deterministic():
    m = meta(EquityEventKind.TRADE)
    assert m.canonical_identity() == m.canonical_identity()
    m.assert_usable_at(T0 + timedelta(seconds=3))
    with pytest.raises(ValueError, match="not available"):
        m.assert_usable_at(T0 + timedelta(seconds=1))


def test_equity_meta_rejects_impossible_clock_and_invalid_hash():
    with pytest.raises(ValueError, match="event_time"):
        EquityMarketDataMeta(
            venue="X",
            instrument_id="US:AAPL",
            venue_symbol="AAPL",
            kind=EquityEventKind.TRADE,
            event_time=T0 + timedelta(seconds=2),
            published_at=T0 + timedelta(seconds=1),
            available_at=T0 + timedelta(seconds=3),
            received_at=T0 + timedelta(seconds=4),
            source_channel="fixture",
            source_sequence=None,
            raw_sha256=RAW,
        )
    with pytest.raises(ValueError, match="SHA-256"):
        meta(EquityEventKind.TRADE, raw_sha256="not-a-hash")


def test_stock_and_etf_instrument_definition_is_pit_bounded():
    for asset_type in (EquityAssetType.STOCK, EquityAssetType.ETF):
        d = EquityInstrumentDefinition(
            instrument_id=f"US:{asset_type.value}",
            venue="TEST_EXCHANGE",
            venue_symbol=asset_type.value,
            asset_type=asset_type,
            primary_exchange="XNAS",
            quote_currency="USD",
            tick_size=Decimal("0.01"),
            lot_size=Decimal("1"),
            status="ACTIVE",
            effective_from=T0,
            published_at=T0,
            available_at=T0 + timedelta(seconds=1),
            received_at=T0 + timedelta(seconds=2),
            raw_sha256=RAW,
        )
        d.assert_usable_at(T0 + timedelta(seconds=2))
        with pytest.raises(ValueError, match="not available"):
            d.assert_usable_at(T0)


def test_instrument_definition_rejects_invalid_sizes():
    with pytest.raises(ValueError, match="positive"):
        EquityInstrumentDefinition(
            instrument_id="US:AAPL",
            venue="TEST_EXCHANGE",
            venue_symbol="AAPL",
            asset_type=EquityAssetType.STOCK,
            primary_exchange="XNAS",
            quote_currency="USD",
            tick_size=Decimal("0"),
            lot_size=Decimal("1"),
            status="ACTIVE",
            effective_from=T0,
            published_at=T0,
            available_at=T0,
            received_at=T0,
            raw_sha256=RAW,
        )


def test_trade_and_quote_contracts_fail_closed():
    EquityTradeEvent(meta(EquityEventKind.TRADE), "t1", Decimal("100"), Decimal("5"))
    EquityQuoteEvent(meta(EquityEventKind.QUOTE), Decimal("99"), Decimal("100"), Decimal("10"), Decimal("12"))
    with pytest.raises(ValueError, match="positive"):
        EquityTradeEvent(meta(EquityEventKind.TRADE), "t2", Decimal("0"), Decimal("1"))
    with pytest.raises(ValueError, match="crossed"):
        EquityQuoteEvent(meta(EquityEventKind.QUOTE), Decimal("101"), Decimal("100"), Decimal("1"), Decimal("1"))


def test_bar_contract_enforces_time_ohlc_and_volume():
    EquityBarEvent(
        meta(EquityEventKind.BAR),
        T0 - timedelta(minutes=1),
        T0,
        Decimal("100"),
        Decimal("105"),
        Decimal("99"),
        Decimal("103"),
        Decimal("1000"),
    )
    with pytest.raises(ValueError, match="high"):
        EquityBarEvent(
            meta(EquityEventKind.BAR),
            T0 - timedelta(minutes=1),
            T0,
            Decimal("100"),
            Decimal("101"),
            Decimal("99"),
            Decimal("103"),
            Decimal("1000"),
        )
    with pytest.raises(ValueError, match="volume"):
        EquityBarEvent(
            meta(EquityEventKind.BAR),
            T0 - timedelta(minutes=1),
            T0,
            Decimal("100"),
            Decimal("105"),
            Decimal("99"),
            Decimal("103"),
            Decimal("-1"),
        )


def test_corporate_action_contracts_are_explicit():
    CorporateActionEvent(
        meta(EquityEventKind.CORPORATE_ACTION),
        "d1",
        CorporateActionKind.CASH_DIVIDEND,
        T0 + timedelta(days=5),
        cash_amount=Decimal("0.25"),
    )
    CorporateActionEvent(
        meta(EquityEventKind.CORPORATE_ACTION),
        "s1",
        CorporateActionKind.STOCK_SPLIT,
        T0 + timedelta(days=5),
        split_ratio=Decimal("2"),
    )
    CorporateActionEvent(
        meta(EquityEventKind.CORPORATE_ACTION),
        "c1",
        CorporateActionKind.SYMBOL_CHANGE,
        T0 + timedelta(days=5),
        old_symbol="OLD",
        new_symbol="NEW",
    )
    with pytest.raises(ValueError, match="split_ratio"):
        CorporateActionEvent(
            meta(EquityEventKind.CORPORATE_ACTION),
            "s2",
            CorporateActionKind.STOCK_SPLIT,
            T0 + timedelta(days=5),
            split_ratio=Decimal("0"),
        )


class DummyMappedConnector(EquityInstrumentMappedConnector):
    venue = "TEST_EXCHANGE"


def test_symbol_mapping_fails_closed():
    c = DummyMappedConnector({"AAPL": "US:AAPL"})
    assert c.instrument_id("AAPL") == "US:AAPL"
    with pytest.raises(UnknownEquityInstrumentError):
        c.instrument_id("UNKNOWN")


def test_ingestion_clock_fails_closed():
    with pytest.raises(ValueError, match="cannot be after"):
        DummyMappedConnector.assert_ingestion_clock(T0 + timedelta(seconds=2), T0 + timedelta(seconds=1))


class DummyConnector(DummyMappedConnector):
    def subscriptions(self, venue_symbol: str):
        return ()

    def normalize(self, payload, *, available_at, received_at, raw_sha256=None):
        self.assert_ingestion_clock(available_at, received_at)
        symbol = payload["symbol"]
        instrument_id = self.instrument_id(symbol)
        m = EquityMarketDataMeta(
            venue=self.venue,
            instrument_id=instrument_id,
            venue_symbol=symbol,
            kind=EquityEventKind.TRADE,
            event_time=T0,
            published_at=T0,
            available_at=available_at,
            received_at=received_at,
            source_channel="fixture",
            source_sequence=str(payload["sequence"]),
            raw_sha256=raw_sha256 or RAW,
        )
        return (EquityTradeEvent(m, str(payload["trade_id"]), Decimal(str(payload["price"])), Decimal(str(payload["size"]))),)


def test_read_only_collector_preserves_raw_lineage(tmp_path):
    raw = json.dumps({"symbol": "AAPL", "sequence": 1, "trade_id": "t1", "price": "100", "size": "5"}).encode()
    digest = hashlib.sha256(raw).hexdigest()
    store = ImmutableRawStore(tmp_path / "raw")
    c = ReadOnlyEquityMarketCollector(DummyConnector({"AAPL": "US:AAPL"}), store)
    receipt = c.collect(raw, available_at=T0 + timedelta(seconds=1), received_at=T0 + timedelta(seconds=2))
    assert receipt.raw_blob.content_hash == digest
    assert receipt.events[0].meta.raw_sha256 == digest
    assert store.get(digest) == raw


def test_raw_bytes_are_stored_before_json_decode_failure(tmp_path):
    raw = b"not-json"
    digest = hashlib.sha256(raw).hexdigest()
    store = ImmutableRawStore(tmp_path / "raw")
    c = ReadOnlyEquityMarketCollector(DummyConnector({"AAPL": "US:AAPL"}), store)
    with pytest.raises(json.JSONDecodeError):
        c.collect(raw, available_at=T0 + timedelta(seconds=1), received_at=T0 + timedelta(seconds=2))
    assert store.get(digest) == raw


class BadLineageConnector(DummyConnector):
    def normalize(self, payload, *, available_at, received_at, raw_sha256=None):
        return super().normalize(
            payload,
            available_at=available_at,
            received_at=received_at,
            raw_sha256="b" * 64,
        )


def test_collector_rejects_lost_raw_lineage(tmp_path):
    raw = json.dumps({"symbol": "AAPL", "sequence": 1, "trade_id": "t1", "price": "100", "size": "5"}).encode()
    c = ReadOnlyEquityMarketCollector(BadLineageConnector({"AAPL": "US:AAPL"}), ImmutableRawStore(tmp_path / "raw"))
    with pytest.raises(RuntimeError, match="lost immutable raw-payload lineage"):
        c.collect(raw, available_at=T0 + timedelta(seconds=1), received_at=T0 + timedelta(seconds=2))
