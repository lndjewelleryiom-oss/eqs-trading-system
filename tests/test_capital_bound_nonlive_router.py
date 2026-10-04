from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.execution.adapter_contract import PrivateReadSnapshot
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter, VenueOrder, VenueOrderStatus
from quant_system.nonlive.capital_router import CapitalBoundNonLiveRouter
from quant_system.paper import PaperTradingEngine
from quant_system.risk.contracts import LifecycleState, ReservationState
from quant_system.risk.control import CapitalControlService
from quant_system.risk.execution_bridge import ExecutionTruthConsumer
from quant_system.risk.nonlive_bridge import NonLiveCapitalBridge
from quant_system.risk.reservations import RiskReservationStore
from quant_system.runtime import PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode
from quant_system.shadow import ShadowExecutionEngine
from test_pretrade_authority import NOW, mandate, order, state


class ReadOnlyPrivateAdapter:
    venue = "SYNTH"
    trading_capable = False

    def __init__(self, snapshot):
        self.snapshot = snapshot

    def private_read(self):
        return self.snapshot

def assumptions():
    return ExecutionAssumptions(
        Decimal("1"), Decimal("2"), Decimal("1"), Decimal("0"),
        Decimal("0"), Decimal("0"), 0, Decimal("1"),
    )


def paper_engine():
    return PaperTradingEngine(
        ConservativeBarExecutionSimulator(assumptions()),
        initial_cash=Decimal("10000"),
    )


def components(tmp_path, mode):
    reservations = RiskReservationStore(tmp_path / "risk.db")
    bridge = NonLiveCapitalBridge(CapitalControlService(reservations))
    truth = ExecutionTruthConsumer(reservations)
    store = PersistentRuntimeStore(tmp_path / "runtime.db")
    adapter = InMemoryBrokerAdapter()
    if mode == RuntimeMode.PAPER:
        runtime = PersistentPaperShadowRuntime(
            runtime_id="capital-paper", mode=mode, store=store,
            paper_engine=paper_engine(), clock=lambda: NOW,
        )
    else:
        runtime = PersistentPaperShadowRuntime(
            runtime_id="capital-shadow", mode=mode, store=store,
            shadow_engine=ShadowExecutionEngine(
                BrokerGateway(adapter, venue_submission_enabled=False)
            ), clock=lambda: NOW,
        )

    runtime.start()
    router = CapitalBoundNonLiveRouter(
        runtime=runtime, bridge=bridge, truth_consumer=truth
    )
    return reservations, runtime, router, adapter


def shadow_mandate():
    return mandate(
        lifecycle_state=LifecycleState.SHADOW,
        execution_authority=frozenset({"SHADOW"}),
    )


def test_paper_route_requires_and_persists_capital_authority(tmp_path):
    reservations, runtime, router, _ = components(tmp_path, RuntimeMode.PAPER)
    result = router.route(order("2"), mandate(), state(), strategy_version="fixture")
    assert result.authorisation.execution_authority is not None
    assert result.authorised_order is not None
    assert runtime.paper_engine.simulator.pending_count == 1
    reservation = result.authorisation.control.reservation
    assert reservation is not None
    assert reservations.get(reservation.reservation_id).state == ReservationState.RESERVED
    event = next(e for e in runtime.store.load_events(runtime.runtime_id)
                 if e.event_type == "ORDER_EVALUATED")
    assert event.payload["lineage"]["reservation_id"] == str(reservation.reservation_id)
    assert event.payload["lineage"]["execution_state_before_runtime"] == "SUBMISSION_PENDING"

def test_shadow_reservation_releases_only_after_clean_private_reconciliation(tmp_path):
    reservations, runtime, router, adapter = components(tmp_path, RuntimeMode.SHADOW)
    result = router.route(
        order("2"), shadow_mandate(), state(), strategy_version="fixture"
    )
    reservation = result.authorisation.control.reservation
    assert reservation is not None
    assert adapter.submitted == []
    assert reservations.active_notional("PAPER-1") == Decimal("200")

    private = ReadOnlyPrivateAdapter(PrivateReadSnapshot(NOW, {}, ()))
    reconciliation = runtime.reconcile_private_shadow(
        adapter=private, expected_open_order_ids=set(), expected_positions={}
    )
    settled = router.settle_shadow_after_reconciliation(
        reservation.reservation_id,
        reconciliation,
        observed_at=NOW,
        evidence_ref="synthetic-private-read-clean",
    )
    assert settled.state == ReservationState.RELEASED
    assert reservations.active_notional("PAPER-1") == 0
    cursor = reservations.truth_cursor(reservation.reservation_id)
    assert cursor["execution_state"] == "SUPPRESSED"
    assert adapter.submitted == []

def test_shadow_reconciliation_failure_keeps_capital_reserved(tmp_path):
    reservations, runtime, router, adapter = components(tmp_path, RuntimeMode.SHADOW)
    result = router.route(
        order("2"), shadow_mandate(), state(), strategy_version="fixture"
    )
    reservation = result.authorisation.control.reservation
    assert reservation is not None
    unknown = uuid4()
    snapshot = PrivateReadSnapshot(
        NOW, {},
        (VenueOrder(unknown, "venue-unknown", "XYZ",
                    VenueOrderStatus.ACKNOWLEDGED, Decimal("1")),),
    )
    reconciliation = runtime.reconcile_private_shadow(
        adapter=ReadOnlyPrivateAdapter(snapshot),
        expected_open_order_ids=set(),
        expected_positions={},
    )
    assert reconciliation.action.value == "HALT"
    with pytest.raises(RuntimeError, match="SHADOW_RECONCILIATION_NOT_CLEAN"):
        router.settle_shadow_after_reconciliation(
            reservation.reservation_id,
            reconciliation,
            observed_at=NOW,
            evidence_ref="synthetic-private-read-mismatch",
        )
    assert reservations.get(reservation.reservation_id).state == ReservationState.RESERVED
    assert reservations.active_notional("PAPER-1") == Decimal("200")
    assert adapter.submitted == []

def test_mode_mismatch_is_rejected_before_reservation(tmp_path):
    reservations, _, router, _ = components(tmp_path, RuntimeMode.SHADOW)
    with pytest.raises(ValueError, match="EXECUTION_MODE_NOT_AUTHORISED_BY_MANDATE"):
        router.route(order("2"), mandate(), state(), strategy_version="fixture")
    assert reservations.active_notional("PAPER-1") == 0


def test_read_only_reconciliation_rejects_trading_capable_adapter(tmp_path):
    _, runtime, _, _ = components(tmp_path, RuntimeMode.SHADOW)

    class UnsafeAdapter(ReadOnlyPrivateAdapter):
        trading_capable = True

    with pytest.raises(PermissionError, match="READ_ONLY"):
        runtime.reconcile_private_shadow(
            adapter=UnsafeAdapter(PrivateReadSnapshot(NOW, {}, ())),
            expected_open_order_ids=set(),
            expected_positions={},
        )
