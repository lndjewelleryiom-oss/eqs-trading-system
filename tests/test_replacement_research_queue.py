import pytest

from quant_system.performance import PersistentStrategyLifecycle, StrategyLifecycleState
from quant_system.performance.replacement import (
    ReplacementResearchError,
    ReplacementResearchQueue,
    ReplacementRequestState,
)


def _paused(tmp_path):
    life = PersistentStrategyLifecycle(tmp_path / "life.db")
    life.register(
        strategy_id="s1",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial",
        research_evidence_sha256="a" * 64,
    )
    life.transition(
        "s1","v1",StrategyLifecycleState.VALIDATED,
        reasons=("PASS",), genuine_research_gate_passed=True,
    )
    life.transition(
        "s1","v1",StrategyLifecycleState.PAPER_CANARY,
        reasons=("CANARY",), evidence_sha256="b" * 64,
    )
    life.transition(
        "s1","v1",StrategyLifecycleState.PAUSED,
        reasons=("DEGRADED",), evidence_sha256="c" * 64,
    )
    return life


def test_paused_strategy_queues_bounded_replacement(tmp_path):
    life = _paused(tmp_path)
    queue = ReplacementResearchQueue(tmp_path / "replacement.db")
    req = queue.enqueue(
        lifecycle=life,
        strategy_id="s1",
        strategy_version="v1",
        asset_class="crypto",
        family="event-response",
        reason="PERFORMANCE_DEGRADED",
        max_hypotheses=3,
    )
    assert req.state is ReplacementRequestState.QUEUED
    assert req.max_hypotheses == 3
    assert queue.verify_hash_chain()

    duplicate = queue.enqueue(
        lifecycle=life,
        strategy_id="s1",
        strategy_version="v1",
        asset_class="crypto",
        family="event-response",
        reason="PERFORMANCE_DEGRADED",
        max_hypotheses=3,
    )
    assert duplicate.request_id == req.request_id


def test_active_strategy_cannot_trigger_replacement(tmp_path):
    life = _paused(tmp_path)
    life.transition(
        "s1","v1",StrategyLifecycleState.PAPER_CANARY,
        reasons=("REVALIDATED",), evidence_sha256="d" * 64,
    )
    queue = ReplacementResearchQueue(tmp_path / "replacement.db")
    with pytest.raises(ReplacementResearchError, match="PAUSED_OR_RETIRED"):
        queue.enqueue(
            lifecycle=life,
            strategy_id="s1",
            strategy_version="v1",
            asset_class="crypto",
            family="event-response",
            reason="SHOULD_NOT_REPLACE",
        )


def test_replacement_budget_is_bounded(tmp_path):
    life = _paused(tmp_path)
    queue = ReplacementResearchQueue(tmp_path / "replacement.db", max_hypotheses_per_request=4)
    with pytest.raises(ReplacementResearchError, match="HYPOTHESIS_BUDGET_OUT_OF_BOUNDS"):
        queue.enqueue(
            lifecycle=life,
            strategy_id="s1",
            strategy_version="v1",
            asset_class="crypto",
            family="event-response",
            reason="DEGRADED",
            max_hypotheses=5,
        )
