from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import RiskAction, Side
from quant_system.data.equities.models import EquityBarEvent, EquityEventKind, EquityMarketDataMeta
from quant_system.nonlive.equity_paper import EquityPaperAdapter
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

T0=datetime(2026,10,4,12,0,tzinfo=timezone.utc)
SID=UUID("c5230034-f122-4e3f-962f-dc83c248038d")
RAW="a"*64

def bar(start, close="100", *, instrument="US:SPY"):
    end=start+timedelta(minutes=1)
    m=EquityMarketDataMeta(
        venue="YAHOO_CHART",instrument_id=instrument,venue_symbol=instrument.split(":")[-1],
        kind=EquityEventKind.BAR,event_time=end,published_at=end,available_at=end,received_at=end,
        source_channel="fixture",source_sequence=str(int(start.timestamp())),raw_sha256=RAW)
    c=Decimal(close)
    return EquityBarEvent(m,start,end,c,c+Decimal("1"),c-Decimal("1"),c,Decimal("10000"))

def adapter():
    assumptions=ExecutionAssumptions(
        commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),
        latency_ms=1)
    engine=PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions),initial_cash=Decimal("10000"))
    limits=RiskLimits(
        max_order_notional=Decimal("1000"),max_symbol_notional=Decimal("2000"),
        max_strategy_notional=Decimal("2000"),max_gross_notional=Decimal("5000"),
        max_leverage=Decimal("1"),max_daily_loss=Decimal("1000"),
        max_drawdown_fraction=Decimal("0.2"),max_data_age=timedelta(minutes=5))
    return EquityPaperAdapter(paper_engine=engine,risk_engine=RiskEngine(limits)),engine

def snapshot(at):
    return PortfolioRiskSnapshot(
        Decimal("10000"),Decimal("10000"),Decimal("0"),{}, {},Decimal("0"),at)

def context(order, at, *, attrs=None, execution_mode="PAPER"):
    cap=Shared03CapitalState(
        "paper-account","portfolio-1",Decimal("10000"),Decimal("10000"),Decimal("9900"),
        Decimal("100"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),
        at,at+timedelta(minutes=5),True,True)
    res=Shared03ReservationView(
        "res-1",str(order.order_id),"portfolio-1",str(order.strategy_id),order.symbol,"YAHOO_CHART",
        Shared03ReservationState.COMMITTED,Decimal("100"),Decimal("0"),at+timedelta(minutes=5))
    exp=Shared03ExposureView(
        "eq-1",Shared03AssetClass.ETF,str(order.strategy_id),order.symbol,"YAHOO_CHART",Decimal("0"),"USD",
        (attrs if attrs is not None else {"long_short_notional":"0","beta_exposure":"1","liquidity_state":"KNOWN"}))
    return Shared03Context(execution_mode,cap,res,(exp,),True,True,True)

def test_paper_adapter_allows_valid_equity_order_and_fills_only_on_later_bar():
    a,e=adapter(); b1=bar(T0); order=a.order_from_closed_bar(b1,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    result=a.evaluate_and_submit(b1,order,risk_snapshot=snapshot(b1.meta.available_at),shared03_context=context(order,b1.meta.available_at))
    assert result.action==RiskAction.ALLOW and result.submitted_to_paper_engine
    assert a.on_closed_bar(b1)==()
    fills=a.on_closed_bar(bar(T0+timedelta(minutes=1),close="101"))
    assert len(fills)==1 and fills[0].order.order_id==order.order_id
    assert len(e.fills)==1

def test_missing_shared03_context_halts_and_does_not_submit():
    a,e=adapter(); b=bar(T0); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    r=a.evaluate_and_submit(b,order,risk_snapshot=snapshot(b.meta.available_at),shared03_context=None)
    assert r.action==RiskAction.HALT
    assert "SHARED03_CONTEXT_MISSING" in r.reason_codes
    assert e.simulator.pending_count==0

def test_equity_exposure_missing_required_attributes_blocks():
    a,e=adapter(); b=bar(T0); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    c=context(order,b.meta.available_at,attrs={})
    r=a.evaluate_and_submit(b,order,risk_snapshot=snapshot(b.meta.available_at),shared03_context=c)
    assert r.action==RiskAction.BLOCK
    assert any(x.startswith("EQUITY_") for x in r.reason_codes)
    assert e.simulator.pending_count==0

def test_execution_mode_mismatch_halts():
    a,e=adapter(); b=bar(T0); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    # construct a valid context then replace execution mode via object creation; SHARED03 itself rejects LIVE.
    try:
        context(order,b.meta.available_at,execution_mode="LIVE")
    except ValueError as exc:
        assert "NONLIVE" in str(exc)
    else:
        raise AssertionError("LIVE context accepted")

def test_risk_limit_blocks_before_paper_submission():
    a,e=adapter(); b=bar(T0,close="1000"); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("2"))
    r=a.evaluate_and_submit(b,order,risk_snapshot=snapshot(b.meta.available_at),shared03_context=context(order,b.meta.available_at))
    assert r.action==RiskAction.BLOCK
    assert "MAX_ORDER_NOTIONAL" in r.reason_codes
    assert e.simulator.pending_count==0

def test_order_must_bind_to_source_bar():
    a,_=adapter(); b=bar(T0); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("1"))
    other=bar(T0,instrument="US:AAPL")
    try:
        a.evaluate_and_submit(other,order,risk_snapshot=snapshot(other.meta.available_at),shared03_context=context(order,other.meta.available_at))
    except ValueError as exc:
        assert "symbol" in str(exc)
    else:
        raise AssertionError("mismatched source bar accepted")
