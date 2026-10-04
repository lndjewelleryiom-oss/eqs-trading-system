from decimal import Decimal
import pytest
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.shadow.engine import ShadowExecutionEngine
from quant_system.risk.control import CapitalControlService
from quant_system.risk.contracts import LifecycleState
from quant_system.risk.nonlive_bridge import NonLiveCapitalBridge
from quant_system.risk.reservations import RiskReservationStore
from test_pretrade_authority import mandate,state,order

def test_shadow_authorised_order_never_reaches_venue(tmp_path):
    o=order("2")
    shadow_mandate=mandate(lifecycle_state=LifecycleState.SHADOW,execution_authority=frozenset({"SHADOW"}))
    result=NonLiveCapitalBridge(CapitalControlService(
        RiskReservationStore(tmp_path/"s.db"))).authorise(
            o,shadow_mandate,state(),strategy_version="fixture",execution_mode="SHADOW")
    assert result.execution_authority is not None
    adapter=InMemoryBrokerAdapter()
    engine=ShadowExecutionEngine(BrokerGateway(adapter,venue_submission_enabled=False))
    decision=engine.evaluate_order(o)
    assert not decision.submission_result.sent
    assert decision.submission_result.reason == "VENUE_SUBMISSION_DISABLED"
    assert adapter.submitted == []

def test_shadow_restart_rejects_duplicate_order(tmp_path):
    o=order("2")
    engine=ShadowExecutionEngine(BrokerGateway(InMemoryBrokerAdapter(),venue_submission_enabled=False))
    engine.evaluate_order(o); saved=engine.export_state()
    restored=ShadowExecutionEngine(BrokerGateway(InMemoryBrokerAdapter(),venue_submission_enabled=False))
    restored.restore_state(saved)
    with pytest.raises(ValueError,match="duplicate shadow order"):
        restored.evaluate_order(o)

def test_shadow_refuses_gateway_with_submission_enabled():
    with pytest.raises(ValueError,match="submission to be disabled"):
        ShadowExecutionEngine(BrokerGateway(InMemoryBrokerAdapter(),venue_submission_enabled=True))
