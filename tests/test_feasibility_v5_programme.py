from __future__ import annotations

from datetime import datetime,timedelta,timezone
from decimal import Decimal

from quant_system.research.feasibility_archive_v2 import FeasibilityBar,FeasibilityFunding
from quant_system.research.feasibility_v4_programme import V4Bar,V4Month
from quant_system.research.feasibility_v4_strategies import BasisFeature
from quant_system.research.feasibility_v5_programme import V5Bar,V5Month,aggregate_v4_month,programme_next_action,run_v5_campaign
from quant_system.research.feasibility_v5_strategies import SlowFeature,SlowMomentumTrend,SlowDonchianBreakout


def source_month(count=96):
    start=datetime(2023,1,1,tzinfo=timezone.utc); rows=[]; price=Decimal('100')
    for i in range(count):
        open_=price; price+=Decimal('0.1')
        trade=FeasibilityBar(start+timedelta(minutes=5*i),open_,price+Decimal('0.05'),open_-Decimal('0.05'),price,Decimal('10'))
        feat=BasisFeature(Decimal('0.0001'),price*Decimal('1.0001'),price)
        rows.append(V4Bar(trade,feat))
    return V4Month('2023-01',tuple(rows),())


def test_5m_to_4h_aggregation_is_deterministic():
    out=aggregate_v4_month(source_month())
    assert len(out.bars)==2
    first=out.bars[0]
    assert first.trade.open_time==datetime(2023,1,1,tzinfo=timezone.utc)
    assert first.trade.volume==Decimal('480')
    assert first.trade.close==Decimal('104.8')
    assert first.feature.premium_fraction==Decimal('0.0001')
    assert first.feature.mark_index_basis_fraction==Decimal('0.0001')


def test_slow_strategies_are_bounded_and_lower_frequency():
    trend=SlowMomentumTrend(short_lookback_bars=2,long_lookback_bars=4,short_threshold=Decimal('0.001'),long_threshold=Decimal('0.002'),premium_abs_max=Decimal('0.002'),max_holding_bars=3)
    signal=None
    for close in map(Decimal,['100','100.1','100.3','100.6','101']):
        signal=trend.observe(SlowFeature(close,Decimal('0.0001'),Decimal('0')),Decimal('0'))
    assert signal is not None and signal[0]==1
    breakout=SlowDonchianBreakout(entry_lookback_bars=4,exit_lookback_bars=2,premium_abs_max=Decimal('0.002'),max_holding_bars=4)
    out=None
    for close in map(Decimal,['100','101','102','103','104']):
        out=breakout.observe(SlowFeature(close,Decimal('0.0001'),Decimal('0')),Decimal('0'))
    assert out is not None and out[0]==1


def v5_month(month,start,closes):
    bars=[]
    for i,close in enumerate(closes):
        close=Decimal(str(close)); open_=close-Decimal('0.1')
        trade=FeasibilityBar(start+timedelta(hours=4*i),open_,close+Decimal('0.2'),open_-Decimal('0.2'),close,Decimal('1000'))
        bars.append(V5Bar(trade,SlowFeature(close,Decimal('0.0001'),Decimal('0.0001'))))
    return V5Month(month,tuple(bars),())


def campaign():
    c={
      'campaign_id':'FEAS-V5-TEST-001','version':'5.1.0','campaign_number':1,'classification':'EXPLORATORY_NON_EVIDENTIARY',
      'periods':{'training_start':'2023-01-01T00:00:00Z','training_end':'2023-01-31T23:59:59Z','validation_start':'2025-07-01T00:00:00Z','validation_end':'2025-07-31T23:59:59Z','locked_oos_start':'2026-04-01T00:00:00Z','locked_oos_end':'2026-09-21T23:59:59Z','locked_oos_access':'SEALED_NOT_PERMITTED'},
      'coverage':{'imputation_permitted':False,'partial_month_use_permitted':False,'strategy_state_reset_after_excluded_interval':True},
      'execution_model':{'fee_bps_per_side':5,'adverse_slippage_bps_per_side':2,'entry_cutoff_bars_before_segment_end':2},
      'sizing':{'starting_equity':'10000','target_position_notional_fraction':'0.10','max_gross_leverage':'1.00','quantity_step':'0.00001'},
      'strategy':{'family':'SLOW_MOMENTUM_TREND','short_lookback_bars':2,'long_lookback_bars':4,'short_return_threshold_fraction':'0.001','long_return_threshold_fraction':'0.002','premium_abs_max_fraction':'0.002','max_holding_bars':3},
      'programme_exit_rule':{'maximum_campaigns':2,'on_no_candidate':'STOP_V5_AND_REVIEW_SCOPE_WITHOUT_TUNING'}
    }
    return {'campaign':c,'campaign_fingerprint':'f'*64}


def test_v5_replay_remains_nonlive_and_reconciled():
    train=[100+i*0.5 for i in range(30)]; valid=[120+i*0.4 for i in range(30)]
    months=(v5_month('2023-01',datetime(2023,1,1,tzinfo=timezone.utc),train),v5_month('2025-07',datetime(2025,7,1,tzinfo=timezone.utc),valid))
    result=run_v5_campaign(campaign_record=campaign(),months=months,trial_id='v5-test')
    assert result.locked_oos_opened is False
    assert result.broker_submission_enabled is False
    assert result.live_authority is False
    assert result.counts_toward_168h_100_trade_gate is False
    assert result.core_journal_reconciled is True
    assert result.maximum_entry_notional_fraction<=Decimal('0.10')


def test_v5_final_campaign_fires_exit_rule():
    first={'campaign_number':1,'programme_exit_rule':{'maximum_campaigns':2,'on_no_candidate':'STOP_V5_AND_REVIEW_SCOPE_WITHOUT_TUNING'}}
    final={'campaign_number':2,'programme_exit_rule':{'maximum_campaigns':2,'on_no_candidate':'STOP_V5_AND_REVIEW_SCOPE_WITHOUT_TUNING'}}
    assert programme_next_action(first,eligible_managed_paper_candidate=False)=='RETAIN_REJECTED_COUNTED_TRIAL_AND_MOVE_TO_NEXT_PREFROZEN_V5_CAMPAIGN'
    assert programme_next_action(final,eligible_managed_paper_candidate=False)=='STOP_V5_AND_REVIEW_SCOPE_WITHOUT_TUNING'
