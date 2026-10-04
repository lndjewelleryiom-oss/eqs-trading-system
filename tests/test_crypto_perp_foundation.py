from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from quant_system.data.crypto_perps.acceptance import BookSequenceGuard, SequenceIntegrityError, assess_latency
from quant_system.data.crypto_perps.connectors import (
    BinanceUsdmConnector,
    BybitLinearConnector,
    OkxSwapConnector,
    UnknownInstrumentError,
)
from quant_system.data.crypto_perps.models import EventKind, LiquidatedSide, MarketDataMeta, payload_sha256


BASE = datetime(2026, 9, 22, 8, 0, 1, tzinfo=timezone.utc)
RECEIVED = BASE + timedelta(milliseconds=2)


def _binance():
    return BinanceUsdmConnector({"BTCUSDT": "BTC-USDT-PERP:BINANCE_USDM"})


def _bybit():
    return BybitLinearConnector({"BTCUSDT": "BTC-USDT-PERP:BYBIT_LINEAR"})


def _okx():
    return OkxSwapConnector({"BTC-USDT-SWAP": "BTC-USDT-PERP:OKX_SWAP"})


def test_binance_uses_current_split_routes_for_public_and_market_streams():
    subs = _binance().subscriptions("BTCUSDT")
    by_channel = {s.channel: s for s in subs}
    assert by_channel["btcusdt@depth@100ms"].endpoint == "wss://fstream.binance.com/public"
    assert by_channel["btcusdt@aggTrade"].endpoint == "wss://fstream.binance.com/market"
    assert by_channel["btcusdt@markPrice@1s"].route_class == "market"
    assert by_channel["btcusdt@forceOrder"].route_class == "market"


def test_bybit_and_okx_use_read_only_public_market_data_endpoints():
    bybit = _bybit().subscriptions("BTCUSDT")
    okx = _okx().subscriptions("BTC-USDT-SWAP")
    assert all(s.endpoint == "wss://stream.bybit.com/v5/public/linear" for s in bybit)
    assert all(s.endpoint == "wss://ws.okx.com:8443/ws/v5/public" for s in okx)
    assert all(s.route_class == "public" for s in (*bybit, *okx))


def test_unknown_instrument_fails_closed_instead_of_guessing_symbol_identity():
    with pytest.raises(UnknownInstrumentError):
        _binance().subscriptions("DOGEUSDT")


def test_point_in_time_meta_forbids_future_publication_and_future_use():
    event = BASE - timedelta(milliseconds=10)
    published = BASE - timedelta(milliseconds=5)
    meta = MarketDataMeta(
        venue="X", instrument_id="I", venue_symbol="S", kind=EventKind.TRADE,
        event_time=event, published_at=published, available_at=BASE, received_at=RECEIVED,
        source_channel="trade", source_sequence="1", raw_sha256=payload_sha256(b"x"),
    )
    with pytest.raises(ValueError, match="not available"):
        meta.assert_usable_at(BASE - timedelta(microseconds=1))
    meta.assert_usable_at(BASE)
    latency = assess_latency(meta)
    assert latency.publication_to_availability == timedelta(milliseconds=5)
    assert latency.availability_to_ingestion == timedelta(milliseconds=2)

    with pytest.raises(ValueError, match="published_at cannot be after available_at"):
        MarketDataMeta(
            venue="X", instrument_id="I", venue_symbol="S", kind=EventKind.TRADE,
            event_time=event, published_at=BASE + timedelta(seconds=1), available_at=BASE,
            received_at=RECEIVED, source_channel="trade", source_sequence=None,
            raw_sha256=payload_sha256(b"x"),
        )


def test_binance_normalizes_trade_mark_depth_and_liquidation():
    connector = _binance()
    trade = connector.normalize({
        "e": "aggTrade", "E": 1790064000000, "s": "BTCUSDT", "a": 77,
        "p": "65000.5", "q": "0.125", "T": 1790063999998, "m": True,
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert trade.meta.kind == EventKind.TRADE
    assert trade.trade_id == "77" and trade.aggressor_side == "SELL"
    assert trade.price == Decimal("65000.5")

    depth = connector.normalize({
        "e": "depthUpdate", "E": 1790064000000, "T": 1790063999999, "s": "BTCUSDT",
        "U": 100, "u": 102, "pu": 99, "b": [["65000", "1.2"]], "a": [["65001", "0"]],
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert depth.first_sequence == 100 and depth.final_sequence == 102 and depth.previous_sequence == 99
    assert depth.bids[0].quantity == Decimal("1.2")

    state = connector.normalize({
        "e": "markPriceUpdate", "E": 1790064000000, "s": "BTCUSDT",
        "p": "65000", "i": "64995", "r": "0.0001", "T": 1790092800000,
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert state.mark_price == Decimal("65000") and state.funding_rate == Decimal("0.0001")

    liq = connector.normalize({
        "e": "forceOrder", "E": 1790064000000,
        "o": {"s": "BTCUSDT", "S": "SELL", "q": "2", "z": "1.5", "ap": "64000", "T": 1790063999999},
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert liq.liquidated_side == LiquidatedSide.LONG
    assert liq.quantity == Decimal("1.5")


def test_bybit_normalizes_snapshot_batched_trades_state_and_all_liquidations():
    connector = _bybit()
    book = connector.normalize({
        "topic": "orderbook.50.BTCUSDT", "type": "snapshot", "ts": 1790064000000,
        "data": {"s": "BTCUSDT", "b": [["65000", "1"]], "a": [["65001", "2"]], "u": 10, "seq": 20, "cts": 1790063999999},
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert book.meta.kind == EventKind.BOOK_SNAPSHOT and book.final_sequence == 10

    trades = connector.normalize({
        "topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": 1790064000000,
        "data": [
            {"T": 1790063999998, "s": "BTCUSDT", "S": "Buy", "v": "0.1", "p": "65000", "i": "t1", "seq": 1},
            {"T": 1790063999999, "s": "BTCUSDT", "S": "Sell", "v": "0.2", "p": "65001", "i": "t2", "seq": 2},
        ],
    }, available_at=BASE, received_at=RECEIVED)
    assert [x.trade_id for x in trades] == ["t1", "t2"]
    assert [x.aggressor_side for x in trades] == ["BUY", "SELL"]

    state = connector.normalize({
        "topic": "tickers.BTCUSDT", "type": "snapshot", "ts": 1790064000000, "cs": 42,
        "data": {"symbol": "BTCUSDT", "markPrice": "65000", "indexPrice": "64990",
                 "openInterest": "1000", "openInterestValue": "65000000", "fundingRate": "-0.0002",
                 "nextFundingTime": "1790092800000"},
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert state.open_interest == Decimal("1000") and state.funding_rate == Decimal("-0.0002")

    liqs = connector.normalize({
        "topic": "allLiquidation.BTCUSDT", "type": "snapshot", "ts": 1790064000000,
        "data": [{"T": 1790063999900, "s": "BTCUSDT", "S": "Buy", "v": "3", "p": "63000"}],
    }, available_at=BASE, received_at=RECEIVED)
    assert liqs[0].liquidated_side == LiquidatedSide.LONG


def test_okx_normalizes_order_book_trade_and_perpetual_state_channels():
    connector = _okx()
    book = connector.normalize({
        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"}, "action": "snapshot",
        "data": [{"asks": [["65001", "2", "0", "1"]], "bids": [["65000", "1", "0", "1"]],
                  "ts": "1790064000000", "checksum": "123", "prevSeqId": "9", "seqId": "10"}],
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert book.meta.kind == EventKind.BOOK_SNAPSHOT and book.checksum == 123

    trade = connector.normalize({
        "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
        "data": [{"instId": "BTC-USDT-SWAP", "tradeId": "500", "px": "65000", "sz": "4", "side": "buy", "ts": "1790064000000"}],
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert trade.trade_id == "500" and trade.aggressor_side == "BUY"

    funding = connector.normalize({
        "arg": {"channel": "funding-rate", "instId": "BTC-USDT-SWAP"},
        "data": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.00005", "fundingTime": "1790064000000", "nextFundingTime": "1790092800000"}],
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert funding.funding_rate == Decimal("0.00005")

    oi = connector.normalize({
        "arg": {"channel": "open-interest", "instId": "BTC-USDT-SWAP"},
        "data": [{"instId": "BTC-USDT-SWAP", "oi": "10000", "oiUsd": "65000000", "ts": "1790064000000"}],
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert oi.open_interest == Decimal("10000")


def test_order_book_sequence_guard_resets_on_snapshot_and_rejects_gap():
    connector = _okx()
    snapshot = connector.normalize({
        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"}, "action": "snapshot",
        "data": [{"asks": [["2", "1", "0", "1"]], "bids": [["1", "1", "0", "1"]],
                  "ts": "1790064000000", "prevSeqId": "9", "seqId": "10"}],
    }, available_at=BASE, received_at=RECEIVED)[0]
    delta = connector.normalize({
        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"}, "action": "update",
        "data": [{"asks": [["2", "0", "0", "0"]], "bids": [["1.5", "2", "0", "1"]],
                  "ts": "1790064000001", "prevSeqId": "10", "seqId": "11"}],
    }, available_at=BASE + timedelta(milliseconds=1), received_at=RECEIVED + timedelta(milliseconds=1))[0]
    gap = connector.normalize({
        "arg": {"channel": "books", "instId": "BTC-USDT-SWAP"}, "action": "update",
        "data": [{"asks": [], "bids": [["1.6", "2", "0", "1"]],
                  "ts": "1790064000002", "prevSeqId": "99", "seqId": "100"}],
    }, available_at=BASE + timedelta(milliseconds=2), received_at=RECEIVED + timedelta(milliseconds=2))[0]

    guard = BookSequenceGuard()
    guard.accept(snapshot)
    guard.accept(delta)
    with pytest.raises(SequenceIntegrityError, match="previous-sequence mismatch"):
        guard.accept(gap)


def test_raw_hash_and_canonical_identity_are_deterministic_for_same_source_event():
    payload = {"b": 2, "a": 1}
    assert payload_sha256(payload) == payload_sha256({"a": 1, "b": 2})
    event = _binance().normalize({
        "e": "aggTrade", "E": 1790064000000, "s": "BTCUSDT", "a": 77,
        "p": "65000.5", "q": "0.125", "T": 1790063999998, "m": False,
    }, available_at=BASE, received_at=RECEIVED)[0]
    assert len(event.meta.canonical_identity()) == 64


def test_raw_first_collector_links_normalized_event_to_exact_stored_bytes(tmp_path):
    import json
    from quant_system.data.crypto_perps.collector import ReadOnlyMarketCollector
    from quant_system.data.raw_store import ImmutableRawStore

    payload = {
        "e": "aggTrade", "E": 1790064000000, "s": "BTCUSDT", "a": 88,
        "p": "65000", "q": "0.5", "T": 1790063999999, "m": False,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    collector = ReadOnlyMarketCollector(_binance(), ImmutableRawStore(tmp_path / "raw"))
    receipt = collector.collect(raw, available_at=BASE, received_at=RECEIVED)
    assert receipt.events[0].meta.raw_sha256 == receipt.raw_blob.content_hash
    assert collector.raw_store.get(receipt.raw_blob.content_hash) == raw


def test_raw_first_collector_preserves_malformed_payload_before_parse_failure(tmp_path):
    from hashlib import sha256
    from quant_system.data.crypto_perps.collector import ReadOnlyMarketCollector
    from quant_system.data.raw_store import ImmutableRawStore

    raw = b'{"broken":'
    store = ImmutableRawStore(tmp_path / "raw")
    collector = ReadOnlyMarketCollector(_binance(), store)
    with pytest.raises(Exception):
        collector.collect(raw, available_at=BASE, received_at=RECEIVED)
    assert store.get(sha256(raw).hexdigest()) == raw


def test_instrument_definition_is_point_in_time_and_does_not_guess_future_metadata():
    from quant_system.data.crypto_perps.models import PerpetualInstrumentDefinition

    definition = PerpetualInstrumentDefinition(
        instrument_id="BTC-USDT-PERP:BINANCE_USDM",
        venue="BINANCE_USDM",
        venue_symbol="BTCUSDT",
        base_asset="BTC",
        quote_asset="USDT",
        settle_asset="USDT",
        contract_style="LINEAR",
        tick_size=Decimal("0.10"),
        lot_size=Decimal("0.001"),
        contract_value=None,
        status="TRADING",
        effective_from=BASE - timedelta(days=100),
        published_at=BASE - timedelta(milliseconds=10),
        available_at=BASE,
        received_at=RECEIVED,
        raw_sha256=payload_sha256(b"instrument-snapshot"),
    )
    with pytest.raises(ValueError, match="not available"):
        definition.assert_usable_at(BASE - timedelta(microseconds=1))
    definition.assert_usable_at(BASE)


def test_r1_connectors_expose_no_private_or_authenticated_subscription_surface():
    subscriptions = (
        *_binance().subscriptions("BTCUSDT"),
        *_bybit().subscriptions("BTCUSDT"),
        *_okx().subscriptions("BTC-USDT-SWAP"),
    )
    assert all("private" not in sub.endpoint.lower() for sub in subscriptions)
    assert all("api_key" not in str(sub.subscribe_payload).lower() for sub in subscriptions)
