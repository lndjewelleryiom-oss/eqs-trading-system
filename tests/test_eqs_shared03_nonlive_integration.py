from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest

from quant_system.core.enums import OrderType, RiskAction, Side
from quant_system.execution.models import OrderRequest
from quant_system.nonlive.pipeline import NonLiveExecutionPipeline, SignalAction, StrategyDecision
from quant_system.risk.policy import PortfolioRiskSnapshot, RiskDecision
from quant_system.risk.shared03 import (
    SCHEMA_VERSION, Shared03AssetClass, Shared03CapitalState, Shared03Context,
    Shared03ExposureView, Shared03NonLiveGate, Shared03ReservationState, Shared03ReservationView,
)

T0=datetime(2026,9,27,12,0,tzinfo=timezone.utc)
ORDER_ID=UUID("2d66e5f8-d9ef-4b1e-82af-bce775710e40")
STRATEGY_ID=UUID("ba3c5f25-e267-4eb7-a320-d55d5328911e")

class _Meta:
    available_at=T0; event_time=T0
    @staticmethod
    def canonical_identity(): return "evt-1"
class _Event: meta=_Meta()
class _Batch:
    events=(_Event(),); instrument_id="BTCUSDT"; venue="PAPER_VENUE"; decision_time=T0
    fingerprint="batch-fingerprint"; dataset_fingerprints=("dataset-fingerprint",)
    universe_version="universe-v1"; infrastructure_boundary_fingerprint="infra-boundary"
class _Source:
    @staticmethod
    def load_feature_batch(**kwargs): return _Batch()
class _FeatureEngine:
    @staticmethod
    def compute(batch): return SimpleNamespace(manifest=SimpleNamespace(fingerprint="feature-manifest"))
class _Strategy:
    @staticmethod
    def decide(batch,run,*,symbol=None):
        return StrategyDecision(
            strategy_id=STRATEGY_ID,strategy_version="fixture-v1",signal=SignalAction.BUY,
            reason_codes=("FIXTURE",),instrument_id=batch.instrument_id,venue=batch.venue,
            symbol=symbol or batch.instrument_id,decision_time=batch.decision_time,quantity=Decimal("1"),
            reference_price=Decimal("100"),selected_feature_name="fixture",selected_feature_value="1",
            selected_feature_fingerprint="feature-record",feature_manifest_fingerprint=run.manifest.fingerprint,
            feature_input_batch_fingerprint=batch.fingerprint,dataset_fingerprints=batch.dataset_fingerprints,
            universe_version=batch.universe_version,source_event_ids=("evt-1",),source_raw_sha256s=("a"*64,),
            infrastructure_boundary_fingerprint=batch.infrastructure_boundary_fingerprint)
    @staticmethod
    def order_for(decision):
        return OrderRequest(decision.strategy_id,decision.symbol,Side.BUY,decision.quantity,OrderType.MARKET,
                            decision.decision_time,decision.reference_price,order_id=ORDER_ID)
class _Guard:
    @staticmethod
    def validate(batch): return None
class _RiskEngine:
    def __init__(self,action=RiskAction.ALLOW): self.action=action
    def evaluate(self,order,snapshot):
        return RiskDecision(self.action,("WITHIN_LIMITS" if self.action==RiskAction.ALLOW else "LEGACY_BLOCK",))
class _Runtime:
    def __init__(self,mode="PAPER"):
        self.mode=SimpleNamespace(value=mode); self.events=[]; self.submitted=[]; self.halts=[]
    def record_pipeline_event(self,kind,payload): self.events.append((kind,payload))
    def submit_order(self,order,*,lineage): self.submitted.append((order,lineage)); return {"accepted":True}
    def halt(self,reason): self.halts.append(reason)

def _snapshot():
    return PortfolioRiskSnapshot(Decimal("10000"),Decimal("10000"),Decimal("0"),{}, {},Decimal("0"),T0)

def _context(*,exposure=None,reservation_state=Shared03ReservationState.COMMITTED,
             valid_until=None,account_known=True,reconcile=True,execution_mode="PAPER"):
    capital=Shared03CapitalState(
        "paper-account","portfolio-1",Decimal("1000"),Decimal("1000"),Decimal("900"),Decimal("100"),
        Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),Decimal("0"),T0,
        valid_until if valid_until is not None else T0+timedelta(minutes=1),account_known,reconcile)
    reservation=Shared03ReservationView(
        "reservation-1",str(ORDER_ID),"portfolio-1",str(STRATEGY_ID),"BTCUSDT","PAPER_VENUE",
        reservation_state,Decimal("100"),Decimal("0"),T0+timedelta(minutes=1))
    exposure=exposure or Shared03ExposureView(
        "crypto-1",Shared03AssetClass.CRYPTO,str(STRATEGY_ID),"BTCUSDT","PAPER_VENUE",Decimal("0"),"USD")
    return Shared03Context(execution_mode,capital,reservation,(exposure,),True,True,reconcile)

def _pipeline(*,action=RiskAction.ALLOW,runtime=None):
    runtime=runtime or _Runtime()
    return NonLiveExecutionPipeline(
        source=_Source(),feature_engine=_FeatureEngine(),strategy=_Strategy(),risk_engine=_RiskEngine(action),
        runtime=runtime,guard=_Guard(),shared03_gate=Shared03NonLiveGate()),runtime

def _run(pipeline,context):
    return pipeline.run_once(instrument_id="BTCUSDT",venue="PAPER_VENUE",decision_time=T0,
                             risk_snapshot=_snapshot(),shared03_context=context)

def test_shared03_healthy_context_preserves_allow_and_adds_lineage():
    pipeline,runtime=_pipeline(); result=_run(pipeline,_context())
    assert result.risk_decision.action==RiskAction.ALLOW
    assert result.risk_decision.shared03_schema_version==SCHEMA_VERSION
    assert result.risk_decision.capital_state_fingerprint
    assert result.risk_decision.reservation_fingerprint
    assert len(result.risk_decision.exposure_fingerprints)==1
    assert result.risk_decision.shared03_context_fingerprint
    assert result.risk_decision.shared03_reason_codes==("SHARED03_WITHIN_NONLIVE_CONTRACT",)
    assert len(runtime.submitted)==1
    assert runtime.submitted[0][1]["shared03_context_fingerprint"]==result.risk_decision.shared03_context_fingerprint

def test_shared03_missing_context_halts_fail_closed():
    pipeline,runtime=_pipeline(); result=_run(pipeline,None)
    assert result.risk_decision.action==RiskAction.HALT
    assert "SHARED03_CONTEXT_MISSING" in result.risk_decision.reason_codes
    assert runtime.submitted==[] and runtime.halts

def test_shared03_stale_capital_state_halts():
    pipeline,runtime=_pipeline(); result=_run(pipeline,_context(valid_until=T0-timedelta(seconds=1)))
    assert result.risk_decision.action==RiskAction.HALT
    assert "CAPITAL_STATE_STALE_OR_UNBOUNDED" in result.risk_decision.reason_codes
    assert runtime.submitted==[]

def test_shared03_released_reservation_blocks_new_risk():
    pipeline,runtime=_pipeline(); result=_run(pipeline,_context(reservation_state=Shared03ReservationState.RELEASED))
    assert result.risk_decision.action==RiskAction.BLOCK
    assert "RESERVATION_NOT_ACTIVE" in result.risk_decision.reason_codes
    assert runtime.submitted==[]

def test_shared03_fx_requires_currency_decomposition():
    exposure=Shared03ExposureView("fx-1",Shared03AssetClass.FX,str(STRATEGY_ID),"BTCUSDT","PAPER_VENUE",
                                  Decimal("100"),"USD",{})
    pipeline,_=_pipeline(); result=_run(pipeline,_context(exposure=exposure))
    assert result.risk_decision.action==RiskAction.BLOCK
    assert "FX_CURRENCY_DECOMPOSITION_MISSING" in result.risk_decision.reason_codes

def test_shared03_futures_reject_margin_as_economic_exposure():
    exposure=Shared03ExposureView(
        "future-1",Shared03AssetClass.FUTURE,str(STRATEGY_ID),"BTCUSDT","PAPER_VENUE",Decimal("100"),"USD",
        {"contract_multiplier":"50","contract_count":"1","underlying_notional":"100","margin_requirement":"10",
         "expiry":"2026-12-18","economic_exposure_basis":"MARGIN"})
    pipeline,_=_pipeline(); result=_run(pipeline,_context(exposure=exposure))
    assert result.risk_decision.action==RiskAction.BLOCK
    assert "FUTURES_MARGIN_NOT_ECONOMIC_EXPOSURE" in result.risk_decision.reason_codes

def test_shared03_options_unknown_undefined_and_stress_risk_blocks():
    exposure=Shared03ExposureView(
        "option-1",Shared03AssetClass.OPTION,str(STRATEGY_ID),"BTCUSDT","PAPER_VENUE",Decimal("100"),"USD",
        {"premium":"5","delta":"0.5","gamma":"0.1","vega":"1","theta":"-0.1","rho":"0.1",
         "expiry":"2026-12-18","strike":"100","assignment_risk":"KNOWN",
         "undefined_risk_state":"UNKNOWN","stress_loss_state":"UNKNOWN"})
    pipeline,_=_pipeline(); result=_run(pipeline,_context(exposure=exposure))
    assert result.risk_decision.action==RiskAction.BLOCK
    assert "OPTIONS_UNDEFINED_RISK_UNKNOWN" in result.risk_decision.reason_codes
    assert "OPTIONS_STRESS_STATE_UNKNOWN" in result.risk_decision.reason_codes

def test_shared03_never_upgrades_legacy_block():
    pipeline,runtime=_pipeline(action=RiskAction.BLOCK); result=_run(pipeline,_context())
    assert result.risk_decision.action==RiskAction.BLOCK
    assert "LEGACY_BLOCK" in result.risk_decision.reason_codes
    assert runtime.submitted==[]

def test_shared03_context_is_nonlive_only():
    with pytest.raises(ValueError,match="SHARED03_NONLIVE_MODE_REQUIRED"): _context(execution_mode="LIVE")

def test_shared03_context_fingerprint_is_deterministic():
    assert _context().context_fingerprint==_context().context_fingerprint


def test_shared03_exposure_must_bind_to_order_identity():
    exposure=Shared03ExposureView(
        "other-1",Shared03AssetClass.CRYPTO,str(STRATEGY_ID),"OTHER","PAPER_VENUE",Decimal("10"),"USD")
    pipeline,runtime=_pipeline(); result=_run(pipeline,_context(exposure=exposure))
    assert result.risk_decision.action==RiskAction.BLOCK
    assert "EXPOSURE_ORDER_BINDING_MISSING" in result.risk_decision.reason_codes
    assert runtime.submitted==[]
