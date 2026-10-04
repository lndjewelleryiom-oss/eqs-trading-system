from decimal import Decimal

from quant_system.research.feasibility_v4_strategies import (
    BasisFeature,
    FundingBasisConfirmedCarry,
    PremiumBasisMeanReversion,
)


def feature(premium: str, mark: str = "101", index: str = "100") -> BasisFeature:
    return BasisFeature(Decimal(premium), Decimal(mark), Decimal(index))


def test_premium_basis_mean_reversion_uses_prior_window_only():
    strategy = PremiumBasisMeanReversion(
        lookback_bars=20,
        entry_zscore=Decimal("2"),
        exit_zscore=Decimal("0.25"),
        max_holding_bars=12,
    )
    for i in range(20):
        premium = Decimal("0.0001") + Decimal(i) * Decimal("0.000001")
        assert strategy.observe(BasisFeature(premium, Decimal("101"), Decimal("100")), Decimal("0")) is None
    signal = strategy.observe(feature("0.0010"), Decimal("0"))
    assert signal is not None
    assert signal[0] == -1
    assert signal[1] == "PREMIUM_BASIS_DISLOCATION_SHORT"


def test_premium_signal_requires_mark_index_sign_confirmation():
    strategy = PremiumBasisMeanReversion(
        lookback_bars=20,
        entry_zscore=Decimal("2"),
        exit_zscore=Decimal("0.25"),
        max_holding_bars=12,
    )
    for i in range(20):
        strategy.observe(BasisFeature(Decimal("0.0001") + Decimal(i) * Decimal("0.000001"), Decimal("101"), Decimal("100")), Decimal("0"))
    assert strategy.observe(BasisFeature(Decimal("0.0010"), Decimal("99"), Decimal("100")), Decimal("0")) is None


def test_premium_strategy_exits_after_mean_reversion():
    strategy = PremiumBasisMeanReversion(
        lookback_bars=20,
        entry_zscore=Decimal("2"),
        exit_zscore=Decimal("0.25"),
        max_holding_bars=12,
    )
    values = [Decimal("0.0001") + Decimal(i) * Decimal("0.000001") for i in range(20)]
    for value in values:
        strategy.observe(BasisFeature(value, Decimal("101"), Decimal("100")), Decimal("0"))
    assert strategy.observe(feature("0.0010"), Decimal("0"))[0] == -1
    mean_like = sum(values, Decimal("0")) / Decimal(len(values))
    exit_signal = strategy.observe(BasisFeature(mean_like, Decimal("100.01"), Decimal("100")), Decimal("-1"))
    assert exit_signal is not None
    assert exit_signal[0] == 0


def test_funding_carry_requires_three_prints_and_both_basis_confirmations():
    strategy = FundingBasisConfirmedCarry(minimum_same_sign_funding_prints=3, max_holding_bars=10)
    aligned = BasisFeature(Decimal("0.0004"), Decimal("101"), Decimal("100"))
    for _ in range(2):
        strategy.on_funding(Decimal("0.0001"))
        assert strategy.observe(aligned, Decimal("0")) is None
    strategy.on_funding(Decimal("0.0001"))
    signal = strategy.observe(aligned, Decimal("0"))
    assert signal is not None
    assert signal[0] == -1
    assert signal[1] == "FUNDING_BASIS_CONFIRMED_ENTRY"


def test_funding_carry_blocks_entry_if_premium_or_basis_disagrees():
    strategy = FundingBasisConfirmedCarry(minimum_same_sign_funding_prints=3, max_holding_bars=10)
    for _ in range(3):
        strategy.on_funding(Decimal("0.0001"))
    assert strategy.observe(BasisFeature(Decimal("-0.0004"), Decimal("101"), Decimal("100")), Decimal("0")) is None
    assert strategy.observe(BasisFeature(Decimal("0.0004"), Decimal("99"), Decimal("100")), Decimal("0")) is None


def test_funding_carry_exits_when_alignment_breaks_and_does_not_use_live_authority():
    strategy = FundingBasisConfirmedCarry(minimum_same_sign_funding_prints=3, max_holding_bars=10)
    aligned = BasisFeature(Decimal("0.0004"), Decimal("101"), Decimal("100"))
    for _ in range(3):
        strategy.on_funding(Decimal("0.0001"))
    assert strategy.observe(aligned, Decimal("0"))[0] == -1
    exit_signal = strategy.observe(BasisFeature(Decimal("-0.0004"), Decimal("101"), Decimal("100")), Decimal("-1"))
    assert exit_signal is not None
    assert exit_signal[0] == 0
