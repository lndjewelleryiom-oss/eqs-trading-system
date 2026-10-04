from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID
import lzma
import struct

import pytest

from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.spot_commodity_simulator import ConservativeSpotCommodityBarExecutionSimulator
from quant_system.core.enums import RiskAction, Side
from quant_system.data.commodities.dukascopy import DukascopySpotCommoditySource
from quant_system.data.commodities.models import SpotCommodityBar
from quant_system.nonlive.spot_commodity_paper import SpotCommodityPaperAdapter
from quant_system.paper.engine import PaperTradingEngine
from quant_system.risk.policy import PortfolioRiskSnapshot, RiskEngine, RiskLimits
from quant_system.risk.shared03 import (
    Shared03AssetClass,
    Shared03CapitalState,
    Shared03Context,
    Shared03ExposureView,
    Shared03ReservationState,
    Shared03ReservationView,
)

T0=datetime(2026,10,2,12,0,tzinfo=timezone.utc)
SID=UUID("6ade4193-e6d1-4bff-9c0f-6b44946e3cfb")
RAW="a"*64


def bar(start=T0,close="4182"):
    c=Decimal(close)
    return SpotCommodityBar(
        symbol="XAUUSD",underlying="XAU",quote_currency="USD",unit="TROY_OUNCE",
        bar_start=start,bar_end=start+timedelta(minutes=5),available_at=start+timedelta(minutes=5),
        open=c,high=c+Decimal("2"),low=c-Decimal("2"),close=c,
        source="fixture",raw_sha256=RAW)


def adapter():
    assumptions=ExecutionAssumptions(
        commission_bps=Decimal("0"),spread_bps=Decimal("3"),slippage_bps=Decimal("2"),
        impact_bps=Decimal("0"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),
        latency_ms=1)
    engine=PaperTradingEngine(
        ConservativeSpotCommodityBarExecutionSimulator(assumptions),initial_cash=Decimal("10000")
    )
    limits=RiskLimits(
        max_order_notional=Decimal("10000"),max_symbol_notional=Decimal("10000"),
        max_strategy_notional=Decimal("10000"),max_gross_notional=Decimal("20000"),
        max_leverage=Decimal("2"),max_daily_loss=Decimal("1000"),
        max_drawdown_fraction=Decimal("0.2"),max_data_age=timedelta(minutes=10))
    return SpotCommodityPaperAdapter(paper_engine=engine,risk_engine=RiskEngine(limits)),engine


def snapshot(at):
    return PortfolioRiskSnapshot(
        Decimal("10000"),Decimal("10000"),Decimal("0"),{}, {},Decimal("0"),at)


def context(order,at,attrs=None):
    capital=Shared03CapitalState(
        "paper-account","portfolio-1",Decimal("10000"),Decimal("10000"),Decimal("5000"),
        Decimal("5000"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),
        at,at+timedelta(minutes=10),True,True)
    reservation=Shared03ReservationView(
        "xau-res",str(order.order_id),"portfolio-1",str(order.strategy_id),order.symbol,"DUKASCOPY",
        Shared03ReservationState.COMMITTED,Decimal("5000"),Decimal("0"),at+timedelta(minutes=10))
    if attrs is None:
        attrs={
            "instrument_form":"SPOT",
            "unit":"TROY_OUNCE",
            "quote_currency":"USD",
            "underlying_notional":str(order.notional),
            "liquidity_state":"KNOWN",
        }
    exposure=Shared03ExposureView(
        "xau-exp",Shared03AssetClass.COMMODITY,str(order.strategy_id),order.symbol,
        "DUKASCOPY",order.notional,"USD",attrs)
    return Shared03Context("PAPER",capital,reservation,(exposure,),True,True,True)


def test_spot_commodity_bar_identity():
    b=bar()
    assert b.instrument_id=="COMMODITY:SPOT:XAUUSD"
    assert b.canonical_identity()==b.canonical_identity()


def test_spot_commodity_bar_rejects_symbol_mismatch():
    with pytest.raises(ValueError,match="symbol"):
        SpotCommodityBar(
            symbol="XAGUSD",underlying="XAU",quote_currency="USD",unit="TROY_OUNCE",
            bar_start=T0,bar_end=T0+timedelta(minutes=5),available_at=T0+timedelta(minutes=5),
            open=Decimal("1"),high=Decimal("1"),low=Decimal("1"),close=Decimal("1"),
            source="fixture",raw_sha256=RAW)


def test_shared03_accepts_spot_commodity_without_futures_expiry_or_margin():
    a,_=adapter(); b=bar()
    order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    c=context(order,b.available_at)
    assert c.exposures[0].validate()==()


def test_shared03_rejects_unknown_spot_commodity_form():
    a,_=adapter(); b=bar()
    order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    c=context(order,b.available_at,attrs={})
    reasons=c.exposures[0].validate()
    assert "COMMODITY_INSTRUMENT_FORM_MISSING_OR_UNSUPPORTED" in reasons


def test_valid_xauusd_paper_order_fills_only_next_bar():
    a,e=adapter(); b1=bar()
    order=a.order_from_closed_bar(b1,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    r=a.evaluate_and_submit(b1,order,risk_snapshot=snapshot(b1.available_at),shared03_context=context(order,b1.available_at))
    assert r.action==RiskAction.ALLOW and r.submitted_to_paper_engine
    assert a.on_closed_bar(b1)==()
    fills=a.on_closed_bar(bar(T0+timedelta(minutes=5),close="4183"))
    assert len(fills)==1
    assert fills[0].fill.impact_bps_applied==Decimal("0")
    assert fills[0].fill.spread_bps_applied>0
    assert len(e.fills)==1


def test_missing_shared03_context_halts_xauusd():
    a,e=adapter(); b=bar()
    order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    r=a.evaluate_and_submit(b,order,risk_snapshot=snapshot(b.available_at),shared03_context=None)
    assert r.action==RiskAction.HALT
    assert e.simulator.pending_count==0


def test_dukascopy_source_url_uses_zero_based_month():
    s=DukascopySpotCommoditySource(
        symbol="XAUUSD",underlying="XAU",quote_currency="USD",unit="TROY_OUNCE",
        price_divisor=Decimal("1000"))
    assert s.daily_url(date(2026,10,2)).endswith("/2026/09/02_ticks.bi5")


def test_dukascopy_tick_decoder_and_aggregation():
    s=DukascopySpotCommoditySource(
        symbol="XAUUSD",underlying="XAU",quote_currency="USD",unit="TROY_OUNCE",
        price_divisor=Decimal("1000"))
    records=[
        struct.pack(">IIIff",0,4182265,4181585,1.0,1.0),
        struct.pack(">IIIff",60000,4182300,4181600,1.0,1.0),
        struct.pack(">IIIff",300000,4182400,4181700,1.0,1.0),
        struct.pack(">IIIff",360000,4182500,4181800,1.0,1.0),
    ]
    raw=lzma.compress(b"".join(records))
    ticks=s.decode_ticks(raw,date(2026,10,2))
    assert len(ticks)==4
    bars=s.aggregate_bars(ticks,raw_sha256=s.sha256(raw),available_at=datetime(2026,10,2,0,11,tzinfo=timezone.utc),minutes=5)
    assert len(bars)==2
    assert bars[0].instrument_id=="COMMODITY:SPOT:XAUUSD"


def test_dukascopy_decoder_rejects_crossed_quotes():
    s=DukascopySpotCommoditySource(
        symbol="XAUUSD",underlying="XAU",quote_currency="USD",unit="TROY_OUNCE",
        price_divisor=Decimal("1000"))
    raw=lzma.compress(struct.pack(">IIIff",0,4181000,4182000,1.0,1.0))
    with pytest.raises(ValueError,match="crossed"):
        s.decode_ticks(raw,date(2026,10,2))
