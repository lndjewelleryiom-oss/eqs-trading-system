from decimal import Decimal

from quant_system.performance import PersistentStrategyLifecycle, StrategyLifecycleState
from quant_system.performance.allocation import (
    AutonomousPaperAllocator,
    PaperAllocationCandidate,
)
from quant_system.regime.models import MarketRegime


def _life(tmp_path, strategy_id, state):
    life = PersistentStrategyLifecycle(tmp_path / "life.db")
    life.register(
        strategy_id=strategy_id,
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial-" + strategy_id,
        research_evidence_sha256="a" * 64,
    )
    life.transition(
        strategy_id,"v1",StrategyLifecycleState.VALIDATED,
        reasons=("PASS",),genuine_research_gate_passed=True,
    )
    if state in {
        StrategyLifecycleState.PAPER_CANARY,
        StrategyLifecycleState.PAPER_ACTIVE,
        StrategyLifecycleState.REDUCED,
        StrategyLifecycleState.PAUSED,
    }:
        life.transition(
            strategy_id,"v1",StrategyLifecycleState.PAPER_CANARY,
            reasons=("CANARY",),evidence_sha256="b"*64,
        )
    if state is StrategyLifecycleState.PAPER_ACTIVE:
        life.transition(
            strategy_id,"v1",StrategyLifecycleState.PAPER_ACTIVE,
            reasons=("ACTIVE",),evidence_sha256="c"*64,
        )
    elif state is StrategyLifecycleState.REDUCED:
        life.transition(
            strategy_id,"v1",StrategyLifecycleState.REDUCED,
            reasons=("REDUCED",),evidence_sha256="c"*64,
        )
    elif state is StrategyLifecycleState.PAUSED:
        life.transition(
            strategy_id,"v1",StrategyLifecycleState.PAUSED,
            reasons=("PAUSED",),evidence_sha256="c"*64,
        )
    return life


def _candidate(strategy_id, base=0.10):
    return PaperAllocationCandidate(
        strategy_id=strategy_id,
        strategy_version="v1",
        researched_base_weight=base,
        confidence=1.0,
        expected_volatility=0.10,
        drawdown_fraction=0.0,
        liquidity_score=1.0,
        capacity_notional=Decimal("1000000"),
        regime_fit={MarketRegime.RISK_ON: 1.0},
    )


def test_canary_is_capped_below_researched_weight(tmp_path):
    life = _life(tmp_path, "s1", StrategyLifecycleState.PAPER_CANARY)
    allocator = AutonomousPaperAllocator(canary_weight_cap=0.02)
    out = allocator.allocate(
        lifecycle=life,
        candidates=[_candidate("s1", 0.10)],
        regime=MarketRegime.RISK_ON,
        regime_confidence=1.0,
        nav=Decimal("10000"),
    )
    assert out.result.weights["s1"] <= 0.02
    assert out.enforced_caps["s1"] == 0.02


def test_paused_strategy_gets_zero_weight(tmp_path):
    life = _life(tmp_path, "s1", StrategyLifecycleState.PAUSED)
    allocator = AutonomousPaperAllocator()
    out = allocator.allocate(
        lifecycle=life,
        candidates=[_candidate("s1")],
        regime=MarketRegime.RISK_ON,
        regime_confidence=1.0,
        nav=Decimal("10000"),
    )
    assert out.result.weights["s1"] == 0.0
    assert out.excluded_strategies["s1"] == "LIFECYCLE_PAUSED"


def test_reduced_state_cannot_restore_full_researched_risk(tmp_path):
    life = _life(tmp_path, "s1", StrategyLifecycleState.REDUCED)
    allocator = AutonomousPaperAllocator(reduced_state_multiplier=0.40)
    out = allocator.allocate(
        lifecycle=life,
        candidates=[_candidate("s1", 0.10)],
        regime=MarketRegime.RISK_ON,
        regime_confidence=1.0,
        nav=Decimal("10000"),
    )
    assert out.result.weights["s1"] <= 0.04
    assert out.enforced_caps["s1"] == 0.04
