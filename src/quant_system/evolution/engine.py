from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .anomalies import Anomaly
from .budget import BudgetLedger
from .evaluator import falsify
from .factory import HypothesisFactory, StrategyBlueprint
from .models import HypothesisRecord, HypothesisStatus, ResearchEvidence
from .registry import HypothesisRegistry


class EvidenceRunner(Protocol):
    def run(self, blueprint: StrategyBlueprint) -> ResearchEvidence: ...


@dataclass(frozen=True, slots=True)
class EvolutionResult:
    record: HypothesisRecord
    blueprint: StrategyBlueprint


class EvolutionEngine:
    """Bounded autonomous research loop. It can create/reject candidates, never deploy capital."""

    def __init__(self, registry: HypothesisRegistry, budget: BudgetLedger, factory: HypothesisFactory):
        self.registry = registry
        self.budget = budget
        self.factory = factory

    def investigate(
        self,
        anomaly: Anomaly,
        *,
        asset_class: str,
        horizon: str,
        runner: EvidenceRunner,
        parameter_combinations: int = 1,
    ) -> EvolutionResult:
        self.budget.reserve_hypothesis()
        hypothesis = self.factory.from_anomaly(anomaly, asset_class=asset_class, horizon=horizon)
        self.registry.register(hypothesis)
        self.registry.transition(hypothesis.hypothesis_id, HypothesisStatus.QUEUED, reasons=("ANOMALY_QUEUED",))
        blueprint = self.factory.blueprint(hypothesis)
        self.budget.reserve_trial(str(hypothesis.hypothesis_id), parameter_combinations=parameter_combinations)
        self.registry.transition(hypothesis.hypothesis_id, HypothesisStatus.TESTING, reasons=("FALSIFICATION_STARTED",))
        try:
            evidence = runner.run(blueprint, criteria=hypothesis.criteria)
        except TypeError as exc:
            if "criteria" not in str(exc):
                raise
            evidence = runner.run(blueprint)
        decision = falsify(evidence, hypothesis.criteria)
        target = HypothesisStatus.CANDIDATE if decision.passed else HypothesisStatus.REJECTED
        record = self.registry.transition(hypothesis.hypothesis_id, target, reasons=decision.reasons, evidence=evidence)
        return EvolutionResult(record, blueprint)
