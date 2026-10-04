from decimal import Decimal

import pytest

from quant_system.monitoring.degradation import ExpectedBehavior, ObservedBehavior
from quant_system.performance.controller import (
    PerformanceControllerError,
    PersistentPerformanceController,
    StrategyPerformanceInput,
)
from quant_system.performance.lifecycle import (
    PersistentStrategyLifecycle,
    StrategyLifecycleState,
)
from quant_system.regime.models import MarketRegime


def _lifecycle_canary(tmp_path, sid="s1"):
    lifecycle = PersistentStrategyLifecycle(tmp_path / "lifecycle.db")
    lifecycle.register(
        strategy_id=sid,
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial-1",
        research_evidence_sha256="a" * 64,
        research_manifest_sha256="b" * 64,
    )
    lifecycle.transition(
        sid,
        "v1",
        StrategyLifecycleState.VALIDATED,
        reasons=("PASS",),
        genuine_research_gate_passed=True,
    )
    lifecycle.transition(
        sid,
        "v1",
        StrategyLifecycleState.PAPER_CANARY,
        reasons=("CANARY_PASS",),
        evidence_sha256="c" * 64,
    )
    return lifecycle


def _expected():
    return ExpectedBehavior(
        win_rate=0.55,
        expectancy=0.01,
        sharpe=1.5,
        max_drawdown=0.10,
        slippage_bps=5.0,
        trades_per_period=20.0,
    )


def _observed_ok():
    return ObservedBehavior(
        win_rate=0.54,
        expectancy=0.0095,
        sharpe=1.45,
        drawdown=0.03,
        slippage_bps=5.2,
        trades_per_period=20.0,
    )


def _input(sid="s1", observed=None, forward=True, **kwargs):
    return StrategyPerformanceInput(
        strategy_id=sid,
        strategy_version="v1",
        expected=_expected(),
        observed=observed or _observed_ok(),
        base_weight=0.10,
        confidence=0.9,
        expected_volatility=0.12,
        liquidity_score=0.9,
        capacity_notional=Decimal("100000"),
        regime_fit={MarketRegime.TRENDING: 1.0},
        forward_paper_acceptance_passed=forward,
        **kwargs,
    )


def _run(controller, inputs):
    return controller.run_cycle(
        inputs,
        regime=MarketRegime.TRENDING,
        regime_confidence=0.9,
        nav=Decimal("10000"),
        broker_submission_enabled=False,
        live_authority=False,
        paper_authorised=True,
    )


def test_canary_promotes_to_paper_active_after_forward_acceptance(tmp_path):
    lifecycle = _lifecycle_canary(tmp_path)
    controller = PersistentPerformanceController(
        tmp_path / "controller.db", lifecycle=lifecycle
    )
    cycle = _run(controller, [_input()])
    decision = cycle.decisions[0]
    assert decision.lifecycle_before is StrategyLifecycleState.PAPER_CANARY
    assert decision.lifecycle_after is StrategyLifecycleState.PAPER_ACTIVE
    assert cycle.allocation.weights["s1"] > 0
    assert cycle.allocation.weights["s1"] <= 0.10
    assert controller.verify_hash_chain()


def test_degradation_reduces_strategy_and_allocation(tmp_path):
    lifecycle = _lifecycle_canary(tmp_path)
    lifecycle.transition(
        "s1",
        "v1",
        StrategyLifecycleState.PAPER_ACTIVE,
        reasons=("FORWARD_PASS",),
        evidence_sha256="d" * 64,
    )
    controller = PersistentPerformanceController(
        tmp_path / "controller.db", lifecycle=lifecycle
    )
    degraded = ObservedBehavior(
        win_rate=0.40,
        expectancy=0.006,
        sharpe=0.9,
        drawdown=0.05,
        slippage_bps=7.0,
        trades_per_period=20.0,
    )
    cycle = _run(controller, [_input(observed=degraded)])
    decision = cycle.decisions[0]
    assert decision.lifecycle_after is StrategyLifecycleState.REDUCED
    assert decision.allocation_multiplier == 0.40
    assert cycle.allocation.weights["s1"] < 0.10


def test_severe_degradation_pauses_and_queues_replacement(tmp_path):
    lifecycle = _lifecycle_canary(tmp_path)
    lifecycle.transition(
        "s1",
        "v1",
        StrategyLifecycleState.PAPER_ACTIVE,
        reasons=("FORWARD_PASS",),
        evidence_sha256="d" * 64,
    )
    controller = PersistentPerformanceController(
        tmp_path / "controller.db", lifecycle=lifecycle
    )
    severe = ObservedBehavior(
        win_rate=0.20,
        expectancy=-0.01,
        sharpe=0.1,
        drawdown=0.15,
        slippage_bps=20.0,
        trades_per_period=5.0,
    )
    cycle = _run(controller, [_input(observed=severe)])
    decision = cycle.decisions[0]
    assert decision.lifecycle_after is StrategyLifecycleState.PAUSED
    assert decision.allocation_multiplier == 0.0
    assert "s1" not in cycle.allocation.weights
    queued = controller.queued_replacements()
    assert len(queued) == 1
    assert queued[0]["strategy_id"] == "s1"


def test_execution_error_pauses_immediately(tmp_path):
    lifecycle = _lifecycle_canary(tmp_path)
    controller = PersistentPerformanceController(
        tmp_path / "controller.db", lifecycle=lifecycle
    )
    cycle = _run(controller, [_input(execution_error=True, forward=False)])
    assert cycle.decisions[0].lifecycle_after is StrategyLifecycleState.PAUSED
    assert cycle.decisions[0].allocation_multiplier == 0.0


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"broker_submission_enabled": True}, "BROKER_SUBMISSION_MUST_REMAIN_DISABLED"),
        ({"live_authority": True}, "LIVE_AUTHORITY_MUST_REMAIN_FALSE"),
        ({"paper_authorised": False}, "PAPER_AUTHORITY_REQUIRED"),
    ],
)
def test_controller_never_crosses_safety_boundaries(tmp_path, kwargs, code):
    lifecycle = _lifecycle_canary(tmp_path)
    controller = PersistentPerformanceController(
        tmp_path / "controller.db", lifecycle=lifecycle
    )
    params = dict(
        inputs=[_input()],
        regime=MarketRegime.TRENDING,
        regime_confidence=0.9,
        nav=Decimal("10000"),
        broker_submission_enabled=False,
        live_authority=False,
        paper_authorised=True,
    )
    params.update(kwargs)
    with pytest.raises(PerformanceControllerError, match=code):
        controller.run_cycle(**params)
