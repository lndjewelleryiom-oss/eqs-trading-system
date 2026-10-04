from datetime import date
from decimal import Decimal
import gzip
import json

from quant_system.research.tardis_provider import (
    TardisProviderAdapter,
    TardisProviderError,
)


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"))


def test_dataset_urls_match_documented_tardis_shape():
    adapter = TardisProviderAdapter()
    assert adapter.dataset_url(
        venue="BINANCE_USDM",
        data_type="liquidations",
        day=date(2023, 1, 1),
        symbol="PERPETUALS",
    ) == (
        "https://datasets.tardis.dev/v1/binance-futures/"
        "liquidations/2023/01/01/PERPETUALS.csv.gz"
    )
    assert adapter.dataset_url(
        venue="OKX_SWAP",
        data_type="derivative_ticker",
        day=date(2023, 1, 1),
        symbol="BTC-USDT-SWAP",
    ) == (
        "https://datasets.tardis.dev/v1/okex-swap/"
        "derivative_ticker/2023/01/01/BTC-USDT-SWAP.csv.gz"
    )


def test_liquidation_parser_and_receipt_are_deterministic():
    raw = _gz(
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "bybit,BTCUSDT,1672531200000000,1672531200000100,l1,sell,16500.5,0.125\n"
    )
    adapter = TardisProviderAdapter()
    rows = adapter.parse_liquidations(raw)
    assert len(rows) == 1
    row = rows[0]
    assert row.exchange == "bybit"
    assert row.symbol == "BTCUSDT"
    assert row.side == "sell"
    assert row.price == Decimal("16500.5")
    assert row.amount == Decimal("0.125")
    assert row.liquidation_id == "l1"

    receipt = adapter.receipt(
        raw,
        venue="BYBIT_LINEAR",
        series="LIQUIDATIONS",
        source_url=adapter.dataset_url(
            venue="BYBIT_LINEAR",
            data_type="liquidations",
            day=date(2023, 1, 1),
            symbol="PERPETUALS",
        ),
        requested_date=date(2023, 1, 1),
        symbol="PERPETUALS",
    )
    assert receipt.classification == "REAL_MARKET"
    assert len(receipt.raw_sha256) == 64
    assert len(receipt.receipt_sha256) == 64


def test_derivative_ticker_parser_extracts_open_interest():
    raw = _gz(
        "exchange,symbol,timestamp,local_timestamp,funding_timestamp,"
        "funding_rate,open_interest,last_price,index_price,mark_price\n"
        "okex-swap,BTC-USDT-SWAP,1672531200000000,1672531200000050,"
        "1672540800000000,0.0001,12345.67,16500,16499.8,16500.1\n"
    )
    rows = TardisProviderAdapter().parse_derivative_ticker(raw)
    assert len(rows) == 1
    row = rows[0]
    assert row.exchange == "okex-swap"
    assert row.open_interest == Decimal("12345.67")
    assert row.funding_rate == Decimal("0.0001")
    assert row.index_price == Decimal("16499.8")
    assert row.mark_price == Decimal("16500.1")


def test_invalid_liquidation_fails_closed():
    raw = _gz(
        "exchange,symbol,timestamp,local_timestamp,id,side,price,amount\n"
        "bybit,BTCUSDT,1672531200000000,1672531200000100,l1,unknown,16500.5,0.125\n"
    )
    try:
        TardisProviderAdapter().parse_liquidations(raw)
    except TardisProviderError as exc:
        assert "invalid liquidation side" in str(exc)
    else:
        raise AssertionError("invalid liquidation side must fail closed")


def test_capability_record_does_not_overclaim_metadata(tmp_path):
    adapter = TardisProviderAdapter()
    assessment = adapter.assess_documented_capability()
    assert assessment.normalized_data_can_cover_missing_market_series is True
    assert assessment.historical_instrument_metadata_certifiable is False
    assert assessment.admission_ready is False
    assert "METADATA" in assessment.metadata_blocker

    out = tmp_path / "capability.json"
    record = adapter.write_capability_record(out)
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded == record
    assert loaded["admission_ready"] is False
    assert loaded["broker_submission_enabled"] is False
    assert loaded["live_authority"] is False
    assert len(loaded["record_sha256"]) == 64
