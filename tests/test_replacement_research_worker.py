from pathlib import Path

from quant_system.performance.lifecycle import (
    PersistentStrategyLifecycle,
    StrategyLifecycleState,
)
from quant_system.performance.replacement import (
    ReplacementRequestState,
    ReplacementResearchQueue,
)
from quant_system.performance.replacement_worker import (
    ReplacementExecutionResult,
    ReplacementResearchWorker,
)


def queued(tmp_path: Path):
    life = PersistentStrategyLifecycle(tmp_path / "life.db")
    # Replacement is only valid for paused/retired, so create a lifecycle
    # path through validated->canary->active->paused.
    record2 = life.register(
        strategy_id="s2",
        strategy_version="v1",
        source_class="GENUINE",
        research_trial_id="trial2",
        research_evidence_sha256="c" * 64,
        research_manifest_sha256="d" * 64,
        asset_class="crypto",
        family="momentum-trend",
    )
    life.transition(
        "s2", "v1", StrategyLifecycleState.VALIDATED,
        reasons=("fixture",), genuine_research_gate_passed=True,
    )
    life.transition("s2", "v1", StrategyLifecycleState.PAPER_CANARY, reasons=("fixture",))
    life.transition("s2", "v1", StrategyLifecycleState.PAPER_ACTIVE, reasons=("fixture",))
    life.transition("s2", "v1", StrategyLifecycleState.PAUSED, reasons=("fixture",))

    queue = ReplacementResearchQueue(tmp_path / "queue.db")
    req = queue.enqueue_for_strategy(
        lifecycle=life,
        strategy_id="s2",
        strategy_version="v1",
        reason="PERFORMANCE_PAUSED",
    )
    return queue, req


def test_worker_claims_and_completes_durable_request(tmp_path):
    queue, req = queued(tmp_path)
    worker = ReplacementResearchWorker(queue, tmp_path / "worker.db")

    outcome = worker.process_one(
        lambda request: ReplacementExecutionResult(
            "COMPLETE", "evidence:fixture-result", "FIXTURE_RESEARCH_COMPLETE"
        )
    )
    assert outcome is not None
    assert outcome.request_id == req.request_id
    assert outcome.worker_result == "COMPLETE"
    assert queue.get(req.request_id).state is ReplacementRequestState.COMPLETED
    assert worker.verify()
    assert worker.process_one(lambda request: None) is None


def test_external_or_data_blocker_is_retained_not_retried_forever(tmp_path):
    queue, req = queued(tmp_path)
    worker = ReplacementResearchWorker(queue, tmp_path / "worker.db")

    outcome = worker.process_one(
        lambda request: ReplacementExecutionResult(
            "BLOCKED", "blocker:R13", "GENUINE_R13_DATA_NOT_ADMITTED"
        )
    )
    assert outcome is not None
    assert queue.get(req.request_id).state is ReplacementRequestState.BLOCKED
    assert worker.process_one(lambda request: None) is None
    assert len(worker.attempts(req.request_id)) == 1


def test_failed_handler_requeues_with_bounded_attempts_then_blocks(tmp_path):
    queue, req = queued(tmp_path)
    worker = ReplacementResearchWorker(queue, tmp_path / "worker.db", max_attempts=2)

    first = worker.process_one(
        lambda request: ReplacementExecutionResult(
            "FAILED", "error:first", "TRANSIENT_FAILURE"
        )
    )
    assert first is not None
    assert queue.get(req.request_id).state is ReplacementRequestState.QUEUED

    second = worker.process_one(
        lambda request: ReplacementExecutionResult(
            "FAILED", "error:second", "TRANSIENT_FAILURE"
        )
    )
    assert second is not None
    assert queue.get(req.request_id).state is ReplacementRequestState.BLOCKED
    assert len(worker.attempts(req.request_id)) == 2
    assert worker.verify()


def test_handler_exception_is_contained_and_does_not_grant_authority(tmp_path):
    queue, req = queued(tmp_path)
    worker = ReplacementResearchWorker(queue, tmp_path / "worker.db", max_attempts=1)

    def explode(request):
        raise RuntimeError("boom")

    outcome = worker.process_one(explode)
    assert outcome is not None
    assert outcome.worker_result == "FAILED"
    assert "HANDLER_EXCEPTION" in outcome.reason
    assert queue.get(req.request_id).state is ReplacementRequestState.BLOCKED
