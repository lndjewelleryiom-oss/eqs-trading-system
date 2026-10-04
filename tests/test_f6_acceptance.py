"""F6 phase exit uses synthetic evidence runners only; no market edge is claimed."""

from quant_system.evolution import (
    Anomaly,
    BudgetLedger,
    EvolutionEngine,
    HypothesisFactory,
    HypothesisRegistry,
    HypothesisStatus,
    ResearchEvidence,
    ResearchTrialBudget,
)


class SequencedRunner:
    def __init__(self, evidence):
        self.evidence = evidence

    def run(self, blueprint):
        return self.evidence


def test_autonomous_loop_creates_tests_rejects_and_retains_failed_hypothesis():
    registry = HypothesisRegistry()
    engine = EvolutionEngine(registry, BudgetLedger(ResearchTrialBudget(max_hypotheses=5, max_trials_per_hypothesis=2)), HypothesisFactory())
    anomaly = Anomaly("EXTREME_ZSCORE", 4.0, -1, "SYNTHETIC")
    fail = ResearchEvidence(-0.001, 0.2, 0.2, 0.5, 0.9, 0.5, True, False, 50)
    result = engine.investigate(anomaly, asset_class="synthetic", horizon="daily", runner=SequencedRunner(fail), parameter_combinations=2)
    assert result.record.status == HypothesisStatus.REJECTED
    assert result.record.evidence == fail
    assert "OOS_MEAN_FAILED" in result.record.reasons
    assert len(registry.records()) == 1


def test_autonomous_loop_can_queue_candidate_but_cannot_deploy_it():
    registry = HypothesisRegistry()
    engine = EvolutionEngine(registry, BudgetLedger(), HypothesisFactory())
    anomaly = Anomaly("CROSS_SECTION_OUTLIER", 3.0, 1, "SYNTHETIC_X")
    good = ResearchEvidence(0.001, 0.8, 0.8, 0.99, 0.2, 0.01, True, True, 100)
    result = engine.investigate(anomaly, asset_class="synthetic", horizon="hourly", runner=SequencedRunner(good))
    assert result.record.status == HypothesisStatus.CANDIDATE
    assert not hasattr(engine, "deploy")
    assert result.record.criteria_fingerprint == result.record.hypothesis.criteria.fingerprint
