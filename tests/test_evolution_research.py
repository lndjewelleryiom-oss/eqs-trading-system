from quant_system.evolution import (
    AcceptanceCriteria,
    Anomaly,
    BudgetLedger,
    CrossSectionalDispersionDetector,
    EvolutionEngine,
    Hypothesis,
    HypothesisFactory,
    HypothesisRegistry,
    HypothesisStatus,
    ResearchEvidence,
    ResearchTrialBudget,
    ZScoreAnomalyDetector,
    falsify,
    prioritize,
)


class FixedRunner:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = 0

    def run(self, blueprint):
        self.calls += 1
        assert blueprint.falsification_tests
        return self.evidence


def passing_evidence():
    return ResearchEvidence(0.001, 0.8, 0.8, 0.99, 0.2, 0.01, True, True, 100)


def failing_evidence():
    return ResearchEvidence(-0.001, 0.3, 0.2, 0.60, 0.9, 0.5, True, False, 100)


def test_zscore_detector_only_emits_extreme_event():
    detector = ZScoreAnomalyDetector(threshold=3.0, min_history=20)
    normal = [0.0] * 20 + [0.0]
    assert detector.detect(normal, context="ABC") is None
    values = [float(i % 2) for i in range(20)] + [10.0]
    anomaly = detector.detect(values, context="ABC")
    assert anomaly is not None
    assert anomaly.kind == "EXTREME_ZSCORE"


def test_cross_section_detector_is_deterministic():
    detector = CrossSectionalDispersionDetector(threshold=1.5)
    first = detector.detect({"a": 0.0, "b": 0.1, "c": 5.0, "d": 0.2})
    second = detector.detect({"d": 0.2, "c": 5.0, "b": 0.1, "a": 0.0})
    assert first == second
    assert first[0].context == "c"


def test_falsification_uses_precommitted_thresholds():
    criteria = AcceptanceCriteria()
    assert falsify(passing_evidence(), criteria).passed
    decision = falsify(failing_evidence(), criteria)
    assert not decision.passed
    assert "OOS_MEAN_FAILED" in decision.reasons
    assert "PBO_FAILED" in decision.reasons


def test_registry_retains_failed_hypothesis_and_blocks_illegal_transition():
    registry = HypothesisRegistry()
    h = Hypothesis("s", "r", "family", "equity", "daily", "synthetic")
    registry.register(h)
    registry.transition(h.hypothesis_id, HypothesisStatus.QUEUED)
    registry.transition(h.hypothesis_id, HypothesisStatus.TESTING)
    registry.transition(h.hypothesis_id, HypothesisStatus.REJECTED, reasons=("FAILED",), evidence=failing_evidence())
    assert registry.get(h.hypothesis_id).status == HypothesisStatus.REJECTED
    try:
        registry.transition(h.hypothesis_id, HypothesisStatus.CANDIDATE)
    except ValueError:
        pass
    else:
        raise AssertionError("rejected strategy cannot jump to candidate")


def test_budget_prevents_unbounded_parameter_search():
    ledger = BudgetLedger(ResearchTrialBudget(max_hypotheses=1, max_trials_per_hypothesis=1, max_parameter_combinations=3))
    ledger.reserve_hypothesis()
    ledger.reserve_trial("h", parameter_combinations=3)
    try:
        ledger.reserve_trial("h")
    except RuntimeError as exc:
        assert "trial budget" in str(exc)
    else:
        raise AssertionError("budget should stop more trials")


def test_priority_is_evidence_weighted_not_return_weighted():
    ranked = prioritize([
        ("strong-evidence", 0.95, 0.2, 0.8, 0.8),
        ("novel-only", 0.30, 1.0, 1.0, 1.0),
    ])
    assert ranked[0].hypothesis_id == "strong-evidence"
