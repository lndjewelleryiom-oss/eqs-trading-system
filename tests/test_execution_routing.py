from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4
from quant_system.core.enums import OrderType, Side
from quant_system.execution.routing import ExecutionAuthority, ExecutionMode, ExecutionRouter, OrderIntent

NOW=datetime(2026,9,26,tzinfo=timezone.utc)
SID=uuid4()
def intent(mode=ExecutionMode.SHADOW):
    return OrderIntent(SID,"v1","p1","OKX","BTC-USDT-SWAP",Side.BUY,Decimal("1"),OrderType.MARKET,NOW,Decimal("100"),"sig","risk","cap",mode)
def auth(*,modes=frozenset({ExecutionMode.SHADOW}),master=False,state="SHADOW"):
    return ExecutionAuthority("a1",SID,"v1",state,modes,frozenset({"OKX"}),frozenset({"BTC-USDT-SWAP"}),"cap","risk-policy",NOW-timedelta(minutes=1),NOW+timedelta(minutes=1),"APPROVED",broker_submission_master_enabled=master)

def test_intent_identity_is_deterministic():
    assert intent().intent_id == intent().intent_id
def test_missing_authority_fails_closed():
    r=ExecutionRouter().authorize(intent(),None,now=NOW); assert not r.allowed and "AUTHORITY_MISSING" in r.reason_codes
def test_shadow_can_authorize_without_broker_master():
    r=ExecutionRouter().authorize(intent(),auth(),now=NOW); assert r.allowed and r.order.order_id==intent().intent_id
def test_live_denied_without_master_and_live_lifecycle():
    a=auth(modes=frozenset({ExecutionMode.LIVE})); r=ExecutionRouter().authorize(intent(ExecutionMode.LIVE),a,now=NOW)
    assert not r.allowed and "BROKER_SUBMISSION_MASTER_DISABLED" in r.reason_codes and "LIVE_LIFECYCLE_NOT_AUTHORISED" in r.reason_codes
def test_live_contract_can_be_logically_authorised_but_does_not_submit():
    a=auth(modes=frozenset({ExecutionMode.LIVE}),master=True,state="LIVE_PROBATION")
    r=ExecutionRouter().authorize(intent(ExecutionMode.LIVE),a,now=NOW); assert r.allowed
def test_kill_and_reconciliation_fail_closed():
    a=auth(); a=ExecutionAuthority(a.authority_id,a.strategy_id,a.strategy_version,a.lifecycle_state,a.allowed_modes,a.venues,a.instruments,a.capital_mandate_ref,a.risk_policy_ref,a.valid_from,a.valid_until,a.review_state,global_kill=True)
    r=ExecutionRouter().authorize(intent(),a,now=NOW,reconciliation_healthy=False); assert not r.allowed and "KILL_ACTIVE" in r.reason_codes and "RECONCILIATION_UNHEALTHY" in r.reason_codes
