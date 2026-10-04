from decimal import Decimal
import pytest
from quant_system.risk.control import CapitalControlService
from quant_system.risk.nonlive_bridge import NonLiveCapitalBridge
from quant_system.risk.reservations import RiskReservationStore
from quant_system.risk.contracts import LifecycleState, RiskDisposition
from test_pretrade_authority import mandate,state,order

def bridge(tmp_path):
    return NonLiveCapitalBridge(CapitalControlService(RiskReservationStore(tmp_path/"b.db")))

def test_paper_new_risk_requires_reservation(tmp_path):
    result=bridge(tmp_path).authorise(order("2"),mandate(),state(),
        strategy_version="fixture",execution_mode="PAPER")
    assert result.control.decision.disposition == RiskDisposition.ALLOW
    assert result.control.reservation is not None
    assert result.execution_authority is not None
    assert result.execution_authority.execution_mode == "PAPER"

def test_shadow_new_risk_requires_reservation(tmp_path):
    shadow_mandate=mandate(lifecycle_state=LifecycleState.SHADOW,execution_authority=frozenset({"SHADOW"}))
    result=bridge(tmp_path).authorise(order("2"),shadow_mandate,state(),
        strategy_version="fixture",execution_mode="SHADOW")
    assert result.execution_authority is not None
    assert result.execution_authority.execution_mode == "SHADOW"

def test_rejected_risk_never_gets_execution_authority(tmp_path):
    result=bridge(tmp_path).authorise(order("2"),mandate(),state(account_truth_known=False),
        strategy_version="fixture",execution_mode="PAPER")
    assert result.control.decision.disposition == RiskDisposition.REJECT
    assert result.execution_authority is None

def test_live_bridge_is_impossible(tmp_path):
    with pytest.raises(ValueError,match="LIVE capital authority is disabled"):
        bridge(tmp_path).authorise(order("2"),mandate(),state(),
            strategy_version="fixture",execution_mode="LIVE")
