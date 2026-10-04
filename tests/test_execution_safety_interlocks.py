from datetime import datetime,timedelta,timezone
from decimal import Decimal
from uuid import uuid4
from quant_system.core.enums import OrderType,Side
from quant_system.execution.routing import ExecutionAuthority,ExecutionMode,OrderIntent
from quant_system.execution.lifecycle import DurableExecutionLedger
from quant_system.execution.safety import ExecutionSafetyInterlock,SafetyInputs,ShadowVenueValidator
from quant_system.execution.adapter_contract import ExchangeAdapterContract,VenueInstrument
NOW=datetime(2026,9,26,12,tzinfo=timezone.utc);SID=uuid4()
def intent(mode=ExecutionMode.LIVE,reduce=False):return OrderIntent(SID,"v","p","OKX","BTC",Side.BUY,Decimal("1"),OrderType.MARKET,NOW,Decimal("10"),"s","r","c",mode,reduce_only=reduce)
def auth():
 return ExecutionAuthority("a",SID,"v","LIVE_PROBATION",frozenset({ExecutionMode.LIVE}),frozenset({"OKX"}),frozenset({"BTC"}),"c","r",NOW-timedelta(seconds=1),NOW+timedelta(seconds=1),"APPROVED",broker_submission_master_enabled=True)
def safety(**kw):
 x=dict(production_environment=True,venue_healthy=True,reconciliation_healthy=True,lifecycle_store_healthy=True,global_safety_known=True,trading_adapter_capable=True);x.update(kw);return SafetyInputs(**x)
def test_each_unknown_or_unhealthy_interlock_denies(tmp_path):
 i=ExecutionSafetyInterlock(DurableExecutionLedger(tmp_path/"x.db"))
 for k in ("venue_healthy","reconciliation_healthy","lifecycle_store_healthy","global_safety_known"):
  assert not i.authorize(intent(),auth(),now=NOW,safety=safety(**{k:False})).allowed
 assert not i.authorize(intent(),auth(),now=NOW,safety=safety(production_environment=False)).allowed
 assert not i.authorize(intent(),auth(),now=NOW,safety=safety(trading_adapter_capable=False)).allowed
def test_unresolved_execution_blocks_risk_increase_but_preserves_reduce_distinction(tmp_path):
 l=DurableExecutionLedger(tmp_path/"x.db");u=uuid4();l.reserve(u,now=NOW)
 for s in ("VALIDATED","RISK_AUTHORISED","ROUTED","SUBMISSION_PENDING"):
  from quant_system.execution.adapter_contract import NormalizedExecutionState as S;l.transition(u,S(s),now=NOW,reason="T")
 gate=ExecutionSafetyInterlock(l)
 assert not gate.authorize(intent(),auth(),now=NOW,safety=safety()).allowed
 # Reduction remains distinguishable; external risk authority still controls whether it may proceed.
 assert gate.authorize(intent(reduce=True),auth(),now=NOW,safety=safety()).allowed
class ReadOnly(ExchangeAdapterContract):
 venue="OKX";trading_capable=False
 def instrument(self,s):return VenueInstrument(s,Decimal("1"),Decimal("1"),Decimal("0.1"),True)
def test_shadow_validation_uses_venue_rules_without_submission():
 a=ReadOnly();x=OrderIntent(SID,"v","p","OKX","BTC",Side.BUY,Decimal("1"),OrderType.LIMIT,NOW,Decimal("10"),"s","r","c",ExecutionMode.SHADOW,limit_price=Decimal("10.0"))
 assert ShadowVenueValidator().validate(x,a)[0]
 assert not hasattr(a,"withdraw")
