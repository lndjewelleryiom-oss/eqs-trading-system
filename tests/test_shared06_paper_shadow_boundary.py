from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from quant_system.backtest.events import BarEvent
from quant_system.backtest.interfaces import ExecutionAssumptions
from quant_system.backtest.simulator import ConservativeBarExecutionSimulator
from quant_system.core.enums import OrderType, Side
from quant_system.execution.broker import BrokerGateway, InMemoryBrokerAdapter
from quant_system.execution.models import OrderRequest
from quant_system.monitoring.shared06 import OperationalCertificationBoundary, ReferenceAggregator
from quant_system.monitoring.shared06.aggregator import sha256_obj
from quant_system.paper import PaperTradingEngine
from quant_system.runtime import PersistentPaperShadowRuntime, PersistentRuntimeStore, RuntimeMode, RuntimeStatus
from quant_system.shadow import ShadowExecutionEngine

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "shared06"
AT = datetime(2026, 9, 27, 17, 0, tzinfo=timezone.utc)


def _load(name: str):
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def _material(mode: str, *, mode_mismatch: bool = False):
    inputs = copy.deepcopy(_load("full_pass_inputs.json"))
    policy = copy.deepcopy(_load("certification_policy.json"))
    policy["environment"] = mode
    policy["gate_policies"]["OPS_G06_MODE_INTEGRITY"]["selectors"][0]["match"]["authorised_mode"] = mode
    for envelope in inputs:
        envelope["environment"] = mode
        payload = envelope["payload"]
        if envelope["record_type"] == "service_health":
            payload["environment"] = mode
            payload["authorised_mode"] = mode
            payload["actual_mode"] = "SHADOW" if mode_mismatch and mode == "PAPER" else mode
        if envelope["record_type"] == "mode_awareness":
            payload["authorised_mode"] = mode
            payload["actual_mode"] = "SHADOW" if mode_mismatch and mode == "PAPER" else mode
            payload["match"] = payload["actual_mode"] == mode
            payload["health_state"] = "HEALTHY" if payload["match"] else "BLOCKED"
            payload["reason_codes"] = [] if payload["match"] else ["MODE_MISMATCH"]
        envelope["payload_sha256"] = sha256_obj(payload)
    policy["policy_sha256"] = sha256_obj(policy, omit=("policy_sha256",))
    return inputs, policy


def _boundary(mode: str, *, mode_mismatch: bool = False) -> OperationalCertificationBoundary:
    inputs, policy = _material(mode, mode_mismatch=mode_mismatch)
    return OperationalCertificationBoundary(
        aggregator=ReferenceAggregator(),
        envelopes_provider=lambda _mode, _at: copy.deepcopy(inputs),
        certification_policies={mode: policy},
    )


def _paper_engine() -> PaperTradingEngine:
    assumptions = ExecutionAssumptions(
        commission_bps=Decimal("1"), spread_bps=Decimal("2"), slippage_bps=Decimal("1"),
        impact_bps=Decimal("1"), financing_bps_annual=Decimal("0"), borrow_bps_annual=Decimal("0"), latency_ms=0,
    )
    return PaperTradingEngine(ConservativeBarExecutionSimulator(assumptions), initial_cash=Decimal("10000"))


def _order() -> OrderRequest:
    return OrderRequest(uuid4(), "BTC-PERP", Side.BUY, Decimal("1"), OrderType.MARKET, AT, Decimal("100"))


def _bar() -> BarEvent:
    return BarEvent("BTC-PERP", AT, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1000"))


def test_reference_boundary_passes_paper_and_produces_verified_lineage(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "paper.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-paper", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine(),
        operational_boundary=_boundary("PAPER"), clock=lambda: AT,
    )
    runtime.start()
    order = _order()
    assert runtime.submit_order(order) == order.order_id
    assert runtime.paper_engine.simulator.has_seen_order(order.order_id)
    decisions = [e for e in store.load_events("shared06-paper") if e.event_type == "OPERATIONAL_CERTIFICATION_DECISION"]
    assert len(decisions) == 1 and decisions[0].payload["allowed"] is True
    evaluated = [e for e in store.load_events("shared06-paper") if e.event_type == "ORDER_EVALUATED"][-1]
    lineage = evaluated.payload["lineage"]
    assert lineage["shared06_runtime_mode"] == "PAPER"
    assert lineage["shared06_broker_submission_enabled"] is False
    assert len(lineage["shared06_certificate_sha256"]) == 64
    assert len(lineage["shared06_seal_sha256"]) == 64


def test_reference_boundary_passes_shadow_but_gateway_remains_zero_submit(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "shadow.db")
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=False)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-shadow", mode=RuntimeMode.SHADOW, store=store,
        shadow_engine=ShadowExecutionEngine(gateway), operational_boundary=_boundary("SHADOW"), clock=lambda: AT,
    )
    runtime.start()
    decision = runtime.submit_order(_order())
    assert decision.submission_result.sent is False
    assert decision.submission_result.reason == "VENUE_SUBMISSION_DISABLED"
    assert gateway.venue_submission_enabled is False
    assert adapter.submitted == []


def test_mode_mismatch_fails_closed_before_paper_engine_mutation(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "mismatch.db")
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-mismatch", mode=RuntimeMode.PAPER, store=store, paper_engine=_paper_engine(),
        operational_boundary=_boundary("PAPER", mode_mismatch=True), clock=lambda: AT,
    )
    runtime.start()
    order = _order()
    with pytest.raises(RuntimeError, match="SHARED06_OPERATIONAL_BLOCK"):
        runtime.submit_order(order)
    assert not runtime.paper_engine.simulator.has_seen_order(order.order_id)
    assert runtime.status == RuntimeStatus.HALTED
    assert "SHARED06_OPERATIONAL_CERTIFICATION_FAIL" in (runtime.halt_reason or "")


def test_shadow_submission_flag_mutation_halts_before_broker_adapter(tmp_path):
    store = PersistentRuntimeStore(tmp_path / "unsafe-shadow.db")
    adapter = InMemoryBrokerAdapter()
    gateway = BrokerGateway(adapter, venue_submission_enabled=False)
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-unsafe-shadow", mode=RuntimeMode.SHADOW, store=store,
        shadow_engine=ShadowExecutionEngine(gateway), operational_boundary=_boundary("SHADOW"), clock=lambda: AT,
    )
    runtime.start()
    gateway.venue_submission_enabled = True
    with pytest.raises(RuntimeError, match="submission enabled"):
        runtime.submit_order(_order())
    assert runtime.status == RuntimeStatus.HALTED
    assert adapter.submitted == []


def test_boundary_rejects_wrong_policy_environment_without_running_execution(tmp_path):
    inputs, paper_policy = _material("PAPER")
    boundary = OperationalCertificationBoundary(
        aggregator=ReferenceAggregator(), envelopes_provider=lambda _mode, _at: inputs,
        certification_policies={"SHADOW": paper_policy},
    )
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-policy-mode", mode=RuntimeMode.SHADOW,
        store=PersistentRuntimeStore(tmp_path / "policy.db"),
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
        operational_boundary=boundary, clock=lambda: AT,
    )
    runtime.start()
    with pytest.raises(RuntimeError, match="CERTIFICATION_POLICY_MODE_MISMATCH"):
        runtime.submit_order(_order())
    assert adapter.submitted == []
    assert runtime.status == RuntimeStatus.HALTED


def test_boundary_directly_rejects_any_submission_enabled_state():
    decision = _boundary("SHADOW").evaluate("SHADOW", broker_submission_enabled=True, certification_as_of=AT)
    assert decision.allowed is False
    assert decision.reason_codes == ("SHARED06_BROKER_SUBMISSION_ENABLED",)
    assert decision.certificate_sha256 is None


def test_paper_rechecks_certificate_before_fill_processing(tmp_path):
    good, policy = _material("PAPER")
    bad, _ = _material("PAPER", mode_mismatch=True)
    state = {"bad": False}
    boundary = OperationalCertificationBoundary(
        aggregator=ReferenceAggregator(),
        envelopes_provider=lambda _mode, _at: copy.deepcopy(bad if state["bad"] else good),
        certification_policies={"PAPER": policy},
    )
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-paper-recheck", mode=RuntimeMode.PAPER,
        store=PersistentRuntimeStore(tmp_path / "bar.db"), paper_engine=_paper_engine(),
        operational_boundary=boundary, clock=lambda: AT,
    )
    runtime.start()
    order = _order()
    runtime.submit_order(order)
    state["bad"] = True
    with pytest.raises(RuntimeError, match="SHARED06_OPERATIONAL_BLOCK"):
        runtime.on_bar(_bar())
    assert runtime.paper_engine.fills == ()
    assert runtime.status == RuntimeStatus.HALTED


def test_shadow_rechecks_certificate_before_reconciliation_read(tmp_path):
    good, policy = _material("SHADOW")
    bad = copy.deepcopy(good)
    for env in bad:
        if env["record_type"] == "mode_awareness":
            env["payload"]["actual_mode"] = "PAPER"
            env["payload"]["match"] = False
            env["payload"]["health_state"] = "BLOCKED"
            env["payload"]["reason_codes"] = ["MODE_MISMATCH"]
            env["payload_sha256"] = sha256_obj(env["payload"])
    state = {"bad": False}
    boundary = OperationalCertificationBoundary(
        aggregator=ReferenceAggregator(),
        envelopes_provider=lambda _mode, _at: copy.deepcopy(bad if state["bad"] else good),
        certification_policies={"SHADOW": policy},
    )
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-shadow-recheck", mode=RuntimeMode.SHADOW,
        store=PersistentRuntimeStore(tmp_path / "recon.db"),
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
        operational_boundary=boundary, clock=lambda: AT,
    )
    runtime.start()
    state["bad"] = True
    with pytest.raises(RuntimeError, match="SHARED06_OPERATIONAL_BLOCK"):
        runtime.reconcile_shadow(expected_open_order_ids=set(), expected_positions={})
    assert adapter.submitted == []
    assert runtime.status == RuntimeStatus.HALTED


def test_orchestrator_calls_execute_certified_cycle_for_paper_order_and_bar(tmp_path):
    boundary = _boundary("PAPER")
    calls = []
    original = boundary.execute_certified_cycle

    def wrapped(*args, **kwargs):
        calls.append(kwargs.get("certification_as_of"))
        return original(*args, **kwargs)

    boundary.execute_certified_cycle = wrapped
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-paper-execute-cycle", mode=RuntimeMode.PAPER,
        store=PersistentRuntimeStore(tmp_path / "execute-cycle.db"), paper_engine=_paper_engine(),
        operational_boundary=boundary, clock=lambda: AT,
    )
    runtime.start()
    runtime.submit_order(_order())
    runtime.on_bar(_bar())
    assert len(calls) == 2


def test_shadow_guard_runs_before_order_and_reconciliation_gateway_paths(tmp_path):
    boundary = _boundary("SHADOW")
    guard_calls = []
    original = boundary.guard_broker_facing_read_or_evaluation

    def wrapped(*args, **kwargs):
        guard_calls.append(kwargs.get("certification_as_of"))
        return original(*args, **kwargs)

    boundary.guard_broker_facing_read_or_evaluation = wrapped
    adapter = InMemoryBrokerAdapter()
    runtime = PersistentPaperShadowRuntime(
        runtime_id="shared06-shadow-guard-calls", mode=RuntimeMode.SHADOW,
        store=PersistentRuntimeStore(tmp_path / "guard-calls.db"),
        shadow_engine=ShadowExecutionEngine(BrokerGateway(adapter, venue_submission_enabled=False)),
        operational_boundary=boundary, clock=lambda: AT,
    )
    runtime.start()
    runtime.submit_order(_order())
    runtime.reconcile_shadow(expected_open_order_ids=set(), expected_positions={})
    assert len(guard_calls) >= 2
    assert adapter.submitted == []
