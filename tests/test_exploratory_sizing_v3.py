from decimal import Decimal
import pytest
from quant_system.research.exploratory_sizing_v3 import ExploratorySizingPolicyV3

def test_quantity_scales_down_as_btc_price_rises():
    p=ExploratorySizingPolicyV3()
    assert p.entry_quantity(Decimal('20000'))==Decimal('0.05000')
    assert p.entry_quantity(Decimal('100000'))==Decimal('0.01000')

def test_entry_notional_never_exceeds_ten_percent_starting_equity():
    p=ExploratorySizingPolicyV3()
    for price in (Decimal('16524.8'),Decimal('50000'),Decimal('123456.78')):
        q=p.entry_quantity(price)
        assert q*price<=Decimal('1000')
        p.assert_entry_within_policy(quantity=q,reference_price=price)

def test_fixed_one_btc_is_rejected_at_realistic_btc_prices():
    p=ExploratorySizingPolicyV3()
    with pytest.raises(PermissionError,match='TARGET_POSITION_NOTIONAL'):
        p.assert_entry_within_policy(quantity=Decimal('1'),reference_price=Decimal('20000'))

def test_policy_is_fail_closed_for_bad_values():
    with pytest.raises(ValueError): ExploratorySizingPolicyV3(target_position_notional_fraction=Decimal('0'))
    with pytest.raises(ValueError): ExploratorySizingPolicyV3(max_gross_leverage=Decimal('1.1'))
    with pytest.raises(ValueError): ExploratorySizingPolicyV3().entry_quantity(Decimal('0'))
