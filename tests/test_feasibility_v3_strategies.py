from decimal import Decimal

from quant_system.research.feasibility_v3_strategies import (
    FundingSignCarry,
    VolatilityCompressionBreakout,
    VolatilityNormalizedMeanReversion,
)


def test_mean_reversion_uses_prior_window_and_exits_near_mean():
    strategy = VolatilityNormalizedMeanReversion(
        lookback_bars=20,
        entry_zscore=Decimal("2"),
        exit_zscore=Decimal("0.5"),
        max_holding_bars=10,
    )
    for value in range(100, 120):
        assert strategy.observe(Decimal(value), Decimal("0")) is None
    signal = strategy.observe(Decimal("130"), Decimal("0"))
    assert signal is not None
    assert signal[0] == -1
    assert signal[1] == "OVERREACTION_SHORT"
    exit_signal = strategy.observe(Decimal("110"), Decimal("-1"))
    assert exit_signal is not None
    assert exit_signal[0] == 0


def test_mean_reversion_does_not_include_current_close_in_entry_threshold():
    strategy = VolatilityNormalizedMeanReversion(
        lookback_bars=20,
        entry_zscore=Decimal("2"),
        exit_zscore=Decimal("0.5"),
        max_holding_bars=10,
    )
    for value in [Decimal("100")] * 19 + [Decimal("101")]:
        strategy.observe(value, Decimal("0"))
    signal = strategy.observe(Decimal("110"), Decimal("0"))
    assert signal is not None
    assert signal[0] == -1


def test_volatility_compression_breakout_requires_compression_then_prior_break():
    strategy = VolatilityCompressionBreakout(
        fast_vol_bars=20,
        slow_vol_bars=40,
        compression_ratio_max=Decimal("0.8"),
        breakout_lookback_bars=10,
        exit_lookback_bars=5,
        max_holding_bars=50,
    )
    # High-volatility first half, then a compressed final segment.
    closes = []
    price = Decimal("100")
    for i in range(21):
        price += Decimal("2") if i % 2 == 0 else Decimal("-2")
        closes.append(price)
    for i in range(20):
        price += Decimal("0.05") if i % 2 == 0 else Decimal("-0.05")
        closes.append(price)
    for close in closes:
        strategy.observe(close, Decimal("0"))
    prior_high = max(closes[-10:])
    signal = strategy.observe(prior_high + Decimal("1"), Decimal("0"))
    assert signal is not None
    assert signal[0] == 1
    assert signal[1] == "COMPRESSION_BREAKOUT_LONG"


def test_funding_sign_carry_enters_receiving_side_after_three_same_sign_prints():
    strategy = FundingSignCarry(minimum_same_sign_prints=3, max_holding_bars=10)
    for _ in range(2):
        strategy.on_funding(Decimal("0.0001"))
        assert strategy.observe(Decimal("100"), Decimal("0")) is None
    strategy.on_funding(Decimal("0.0001"))
    signal = strategy.observe(Decimal("100"), Decimal("0"))
    assert signal is not None
    assert signal[0] == -1
    assert signal[1] == "PERSISTENT_FUNDING_CARRY_ENTRY"


def test_funding_sign_reversal_exits_existing_position():
    strategy = FundingSignCarry(minimum_same_sign_prints=3, max_holding_bars=10)
    for _ in range(3):
        strategy.on_funding(Decimal("0.0001"))
    assert strategy.observe(Decimal("100"), Decimal("0"))[0] == -1
    strategy.on_funding(Decimal("-0.0001"))
    signal = strategy.observe(Decimal("100"), Decimal("-1"))
    assert signal is not None
    assert signal[0] == 0
