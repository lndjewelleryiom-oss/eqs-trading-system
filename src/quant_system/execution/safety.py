from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from quant_system.execution.routing import ExecutionAuthority,ExecutionMode,ExecutionRouter,OrderIntent,RouteDecision
from quant_system.execution.lifecycle import DurableExecutionLedger
from quant_system.execution.adapter_contract import ExchangeAdapterContract

@dataclass(frozen=True,slots=True)
class SafetyInputs:
    production_environment: bool
    venue_healthy: bool
    reconciliation_healthy: bool
    lifecycle_store_healthy: bool
    global_safety_known: bool
    trading_adapter_capable: bool

class ExecutionSafetyInterlock:
    """Consumes external authority/safety state. It does not own capital/lifecycle policy."""
    def __init__(self,ledger:DurableExecutionLedger):
        self.ledger=ledger;self.router=ExecutionRouter()
    def authorize(self,intent:OrderIntent,authority:ExecutionAuthority|None,*,now:datetime,safety:SafetyInputs):
        reasons=[]
        if intent.execution_mode==ExecutionMode.LIVE and not safety.production_environment:reasons.append("NOT_PRODUCTION_ENVIRONMENT")
        if not safety.lifecycle_store_healthy:reasons.append("LIFECYCLE_STORE_UNHEALTHY")
        if not safety.global_safety_known:reasons.append("GLOBAL_SAFETY_UNKNOWN")
        if intent.execution_mode==ExecutionMode.LIVE and not safety.trading_adapter_capable:reasons.append("TRADING_ADAPTER_NOT_CAPABLE")
        if self.ledger.exposure_blocked() and not intent.reduce_only:reasons.append("UNRESOLVED_EXECUTION_BLOCK")
        routed=self.router.authorize(intent,authority,now=now,venue_healthy=safety.venue_healthy,reconciliation_healthy=safety.reconciliation_healthy)
        reasons.extend(routed.reason_codes if not routed.allowed else ())
        return RouteDecision(not reasons,tuple(reasons) or ("AUTHORISED",),routed.order if not reasons else None)

class ShadowVenueValidator:
    """Runs common venue validation with a non-trading adapter; never transmits."""
    def validate(self,intent:OrderIntent,adapter:ExchangeAdapterContract):
        if intent.execution_mode!=ExecutionMode.SHADOW:return False,("SHADOW_MODE_REQUIRED",)
        instrument=adapter.instrument(intent.instrument)
        result=adapter.validate_order(intent.to_order_request(),instrument)
        return result.valid,result.reasons
