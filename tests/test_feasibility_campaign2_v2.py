from decimal import Decimal
import copy
import json
from pathlib import Path

from quant_system.research.feasibility_campaign2_v2 import load_campaign2, validate_campaign2
from quant_system.research.feasibility_strategy_v2 import FixedCloseChannelBreakout

ROOT=Path(__file__).resolve().parents[1]


def contract():
    return json.loads((ROOT/'research'/'preregistrations'/'v2'/'FEAS-BINANCE-BTC-BREAKOUT-002.json').read_text(encoding='utf-8'))


def test_campaign2_frozen_contract_valid():
    record=load_campaign2(ROOT)
    assert record['campaign']['strategy']['family']=='CLOSE_CHANNEL_BREAKOUT'
    assert record['campaign']['research_rules']['campaign1_parameters_modified'] is False
    assert record['campaign']['programme_exit_rule']['campaign_number']==2


def test_campaign2_fingerprint_change_fails():
    record=copy.deepcopy(contract())
    record['campaign']['strategy']['entry_lookback_bars']=289
    assert 'FINGERPRINT_MISMATCH' in validate_campaign2(record)


def test_channel_uses_prior_closes_for_breakout():
    strategy=FixedCloseChannelBreakout(entry_lookback_bars=4,exit_lookback_bars=2,max_holding_bars=8)
    for close in [Decimal('100'),Decimal('101'),Decimal('102'),Decimal('103')]:
        assert strategy.observe(close,Decimal('0')) is None
    signal=strategy.observe(Decimal('104'),Decimal('0'))
    assert signal is not None and signal[0]==1 and signal[1]=='CHANNEL_BREAKOUT_LONG'


def test_channel_exit_and_max_hold_are_bounded():
    strategy=FixedCloseChannelBreakout(entry_lookback_bars=4,exit_lookback_bars=2,max_holding_bars=5)
    for close in [Decimal('100'),Decimal('101'),Decimal('102'),Decimal('103')]:
        strategy.observe(close,Decimal('0'))
    assert strategy.observe(Decimal('104'),Decimal('0'))[0]==1
    # Prior exit window is [103,104], so 102 exits a long.
    out=strategy.observe(Decimal('102'),Decimal('1'))
    assert out is not None and out[0]==0 and out[1]=='CHANNEL_EXIT_LONG'
