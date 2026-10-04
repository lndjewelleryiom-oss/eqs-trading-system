from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

from quant_system.core.enums import Side
from quant_system.risk.authority import PreTradeAuthorityEngine
from quant_system.risk.contracts import RiskDecisionV1, RiskDirection, RiskDisposition, ReservationState
from quant_system.risk.exposure import ExposureAccountant, ExposureLine
from quant_system.risk.interfaces import EQS07ExecutionTruthV1, ExecutionTruth
from quant_system.risk.mandates import MandateRegistry, MandateStatus
from quant_system.risk.reservations import RiskReservationStore
from test_pretrade_authority import NOW, SID, mandate, state, order

def test_exposure_accounting_is_deterministic():
    snap=ExposureAccountant.compute([
        ExposureLine("a","V","X",Decimal("100")),
        ExposureLine("b","V","Y",Decimal("-40"))],
        reserved=Decimal("20"),open_order=Decimal("10"))
    assert snap.gross == 140 and snap.net == 60 and snap.committed_gross == 170
    assert snap.by_strategy == {"a":Decimal("100"),"b":Decimal("40")}

def test_stale_unbounded_truth_fails_closed():
    d=PreTradeAuthorityEngine().evaluate(order(),mandate(),state(truth_valid_until=None))
    assert d.disposition == RiskDisposition.REJECT
    assert "RISK_STATE_STALE_OR_UNBOUNDED" in d.reason_codes

def test_protected_reserve_and_order_rate_enforced():
    reserve=PreTradeAuthorityEngine().evaluate(
        order(),mandate(),state(available_capital=Decimal("1000")))
    rate=PreTradeAuthorityEngine().evaluate(
        order(),mandate(),state(recent_order_count=10))
    assert reserve.disposition == RiskDisposition.REJECT
    assert rate.disposition == RiskDisposition.REJECT
    assert "ORDER_RATE_LIMIT" in rate.reason_codes
def test_net_leverage_and_concentration_reduce_capacity():
    d=PreTradeAuthorityEngine().evaluate(
        order("20"), mandate(max_order_notional=Decimal("5000")),
        state(net_exposure=Decimal("4900"),gross_exposure=Decimal("4900"),
              position_quantity=Decimal("19")))
    assert d.disposition in {RiskDisposition.REDUCE,RiskDisposition.REJECT}

def test_mandate_expiry_and_revocation_block_new_keep_existing():
    reg=MandateRegistry(); m=mandate(); reg.issue(m)
    assert reg.status(m.mandate_id,now=NOW).status == MandateStatus.ACTIVE
    reg.revoke(m.mandate_id,"SAFETY")
    rec=reg.status(m.mandate_id,now=NOW)
    assert rec.status == MandateStatus.REVOKED
    assert reg.reservation_action(rec.status) == "BLOCK_NEW_KEEP_EXISTING_RESERVATIONS"

def _decision():
    return RiskDecisionV1.create(
        intent_id=uuid4(),strategy_id=SID,mandate_id="m",mandate_version=1,
        input_state_ref="s",disposition=RiskDisposition.ALLOW,
        direction=RiskDirection.RISK_INCREASING,requested_quantity=Decimal("2"),
        authorised_quantity=Decimal("2"),reason_codes=("OK",),limits_evaluated=("GROSS",),
        limits_consumed=("GROSS",),decided_at=NOW,risk_engine_version="t",policy_version="t")

def test_eqs07_unknown_then_cancel_controls_release(tmp_path):
    store=RiskReservationStore(tmp_path/"r.db")
    r=store.reserve(_decision(),portfolio_id="p",instrument="X",venue="V",
        reference_price=Decimal("100"),max_total_reserved=Decimal("200"),now=NOW)
    unknown=EQS07ExecutionTruthV1(r.reservation_id,ExecutionTruth.UNKNOWN,
        Decimal("0"),NOW,"synthetic-evidence")
    store.apply_execution_truth(unknown)
    assert store.active_notional("p") == Decimal("200")
    cancel=EQS07ExecutionTruthV1(r.reservation_id,ExecutionTruth.CANCELLED,
        Decimal("0"),NOW+timedelta(seconds=1),"synthetic-reconciled")
    store.apply_execution_truth(cancel)
    assert store.active_notional("p") == 0
