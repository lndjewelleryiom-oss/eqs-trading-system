from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.fx_simulator import ConservativeFxBarExecutionSimulator
from quant_system.core.enums import RiskAction, Side
from quant_system.data.fx.models import FxBar
from quant_system.nonlive.fx_paper import FxPaperAdapter
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

def bar(start=T0, close="1.1000", *, base="EUR", quote="USD"):
    end=start+timedelta(hours=1)
    c=Decimal(close)
    return FxBar(
        pair=base+quote,base_currency=base,quote_currency=quote,
        bar_start=start,bar_end=end,available_at=end,
        open=c,high=c+Decimal("0.0010"),low=c-Decimal("0.0010"),close=c,
        source="fixture",raw_sha256=RAW)

def adapter():
    assumptions=ExecutionAssumptions(
        commission_bps=Decimal("1"),spread_bps=Decimal("2"),slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"),financing_bps_annual=Decimal("0"),borrow_bps_annual=Decimal("0"),
        latency_ms=1)
    engine=PaperTradingEngine(ConservativeFxBarExecutionSimulator(assumptions),initial_cash=Decimal("10000"))
    limits=RiskLimits(
        max_order_notional=Decimal("5000"),max_symbol_notional=Decimal("5000"),
        max_strategy_notional=Decimal("5000"),max_gross_notional=Decimal("10000"),
        max_leverage=Decimal("2"),max_daily_loss=Decimal("1000"),
        max_drawdown_fraction=Decimal("0.2"),max_data_age=timedelta(hours=2))
    return FxPaperAdapter(paper_engine=engine,risk_engine=RiskEngine(limits)),engine

def snapshot(at):
    return PortfolioRiskSnapshot(
        Decimal("10000"),Decimal("10000"),Decimal("0"),{}, {},Decimal("0"),at)

def context(order, at, *, legs=None, execution_mode="PAPER"):
    cap=Shared03CapitalState(
        "paper-account","portfolio-1",Decimal("10000"),Decimal("10000"),Decimal("9900"),
        Decimal("200"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),
        at,at+timedelta(minutes=5),True,True)
    res=Shared03ReservationView(
        "res-1",str(order.order_id),"portfolio-1",str(order.strategy_id),order.symbol,"FX_SIM",
        Shared03ReservationState.COMMITTED,Decimal("200"),Decimal("0"),at+timedelta(minutes=5))
    if legs is None:
        legs=[
            {"currency":"EUR","amount":"100","measurement_status":"KNOWN"},
            {"currency":"USD","amount":"-110","measurement_status":"KNOWN"},
        ]
    exp=Shared03ExposureView(
        "fx-1",Shared03AssetClass.FX,str(order.strategy_id),order.symbol,"FX_SIM",Decimal("110"),"USD",
        {"currency_legs":legs})
    return Shared03Context(execution_mode,cap,res,(exp,),True,True,True)

def test_fx_bar_contract_and_identity():
    b=bar()
    assert b.instrument_id=="FX:EURUSD"
    assert b.canonical_identity()==b.canonical_identity()

def test_fx_bar_rejects_pair_currency_mismatch():
    try:
        FxBar(
            pair="EURUSD",base_currency="GBP",quote_currency="USD",
            bar_start=T0,bar_end=T0+timedelta(hours=1),available_at=T0+timedelta(hours=1),
            open=Decimal("1"),high=Decimal("1"),low=Decimal("1"),close=Decimal("1"),
            source="fixture",raw_sha256=RAW)
    except ValueError as exc:
        assert "pair" in str(exc)
    else:
        raise AssertionError("pair/currency mismatch accepted")

def test_valid_fx_order_is_allowed_and_fills_only_on_later_bar():
    a,e=adapter(); b1=bar(); order=a.order_from_closed_bar(b1,strategy_id=SID,side=Side.BUY,quantity=Decimal("100"))
    r=a.evaluate_and_submit(b1,order,risk_snapshot=snapshot(b1.available_at),shared03_context=context(order,b1.available_at))
    assert r.action==RiskAction.ALLOW and r.submitted_to_paper_engine
    assert a.on_closed_bar(b1)==()
    fills=a.on_closed_bar(bar(T0+timedelta(hours=1),close="1.1010"))
    assert len(fills)==1 and fills[0].order.order_id==order.order_id
    assert fills[0].fill.impact_bps_applied==Decimal("0")
    assert fills[0].fill.spread_bps_applied>Decimal("0")
    assert fills[0].fill.slippage_bps_applied>Decimal("0")
    assert len(e.fills)==1

def test_missing_shared03_context_halts():
    a,e=adapter(); b=bar(); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("100"))
    r=a.evaluate_and_submit(b,order,risk_snapshot=snapshot(b.available_at),shared03_context=None)
    assert r.action==RiskAction.HALT
    assert "SHARED03_CONTEXT_MISSING" in r.reason_codes
    assert e.simulator.pending_count==0

def test_currency_leg_pair_mismatch_blocks():
    a,e=adapter(); b=bar(); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("100"))
    bad=[{"currency":"GBP","amount":"100","measurement_status":"KNOWN"},{"currency":"USD","amount":"-110","measurement_status":"KNOWN"}]
    r=a.evaluate_and_submit(b,order,risk_snapshot=snapshot(b.available_at),shared03_context=context(order,b.available_at,legs=bad))
    assert r.action==RiskAction.BLOCK
    assert "FX_CURRENCY_LEGS_DO_NOT_MATCH_PAIR" in r.reason_codes
    assert e.simulator.pending_count==0

def test_shared03_unknown_currency_leg_blocks():
    a,e=adapter(); b=bar(); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("100"))
    bad=[{"currency":"EUR","amount":"100","measurement_status":"UNKNOWN"},{"currency":"USD","amount":"-110","measurement_status":"KNOWN"}]
    r=a.evaluate_and_submit(b,order,risk_snapshot=snapshot(b.available_at),shared03_context=context(order,b.available_at,legs=bad))
    assert r.action==RiskAction.BLOCK
    assert "FX_CURRENCY_LEG_UNKNOWN" in r.reason_codes
    assert e.simulator.pending_count==0

def test_risk_limit_blocks_before_paper_submission():
    a,e=adapter(); b=bar(close="200"); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("100"))
    r=a.evaluate_and_submit(b,order,risk_snapshot=snapshot(b.available_at),shared03_context=context(order,b.available_at))
    assert r.action==RiskAction.BLOCK
    assert "MAX_ORDER_NOTIONAL" in r.reason_codes
    assert e.simulator.pending_count==0

def test_live_context_is_rejected_by_shared03_contract():
    a,_=adapter(); b=bar(); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("100"))
    try:
        context(order,b.available_at,execution_mode="LIVE")
    except ValueError as exc:
        assert "NONLIVE" in str(exc)
    else:
        raise AssertionError("LIVE context accepted")

def test_order_must_bind_to_source_bar():
    a,_=adapter(); b=bar(); order=a.order_from_closed_bar(b,strategy_id=SID,side=Side.BUY,quantity=Decimal("100"))
    other=bar(base="GBP",quote="USD")
    try:
        a.evaluate_and_submit(other,order,risk_snapshot=snapshot(other.available_at),shared03_context=context(order,other.available_at))
    except ValueError as exc:
        assert "symbol" in str(exc)
    else:
        raise AssertionError("mismatched FX source bar accepted")
