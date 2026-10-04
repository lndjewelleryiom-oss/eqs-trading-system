from datetime import date, timedelta
from decimal import Decimal

from quant_system.research.public_liquidity import (
    DailyQuoteVolume,
    PublicDailyLiquidityClient,
    eligibility_series,
)


def test_eligibility_requires_complete_30d_window_and_90d_history():
    start = date(2022, 9, 1)
    rows = tuple(
        DailyQuoteVolume(
            "BINANCE_USDM",
            "BTCUSDT",
            start + timedelta(days=i),
            Decimal("20000000"),
        )
        for i in range(130)
    )
    series = eligibility_series(rows, available_since=start)
    first_eligible = next(x for x in series if x.eligible_10m)
    assert first_eligible.history_days >= 90
    assert first_eligible.rolling_30d_median_quote_volume_usd == Decimal("20000000")
    assert first_eligible.eligible_25m is False


def test_25m_threshold_is_stricter():
    start = date(2022, 9, 1)
    rows = tuple(
        DailyQuoteVolume(
            "BYBIT_LINEAR",
            "ETHUSDT",
            start + timedelta(days=i),
            Decimal("30000000"),
        )
        for i in range(130)
    )
    series = eligibility_series(rows, available_since=start)
    eligible = [x for x in series if x.eligible_25m]
    assert eligible
    assert all(x.eligible_10m for x in eligible)


def test_binance_parser_uses_quote_volume():
    def requester(url):
        return [
            [1672531200000, "1", "2", "0.5", "1.5", "100", 1672617599999, "12345678", 1, "0", "0", "0"]
        ]
    client = PublicDailyLiquidityClient(requester=requester)
    rows = client.fetch(
        venue="BINANCE_USDM",
        symbol="BTCUSDT",
        start=date(2023,1,1),
        end=date(2023,1,1),
    )
    assert rows[0].quote_volume_usd == Decimal("12345678")


def test_bybit_parser_uses_turnover():
    def requester(url):
        return {
            "retCode": 0,
            "result": {
                "list": [
                    ["1672531200000","1","2","0.5","1.5","100","87654321"]
                ]
            },
        }
    client = PublicDailyLiquidityClient(requester=requester)
    rows = client.fetch(
        venue="BYBIT_LINEAR",
        symbol="BTCUSDT",
        start=date(2023,1,1),
        end=date(2023,1,1),
    )
    assert rows[0].quote_volume_usd == Decimal("87654321")


def test_okx_parser_uses_quote_volume():
    calls = []
    def requester(url):
        calls.append(url)
        if len(calls) == 1:
            return {
                "code": "0",
                "data": [
                    ["1672531200000","1","2","0.5","1.5","100","100","55555555","1"]
                ],
            }
        return {"code":"0","data":[]}
    client = PublicDailyLiquidityClient(requester=requester)
    rows = client.fetch(
        venue="OKX_SWAP",
        symbol="BTC-USDT-SWAP",
        start=date(2023,1,1),
        end=date(2023,1,1),
    )
    assert rows[0].quote_volume_usd == Decimal("55555555")
