from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResearchTrialBudget:
    max_hypotheses: int = 50
    max_trials_per_hypothesis: int = 100
    max_parameter_combinations: int = 1000

    def __post_init__(self) -> None:
        if min(self.max_hypotheses, self.max_trials_per_hypothesis, self.max_parameter_combinations) <= 0:
            raise ValueError("research budget limits must be positive")


class BudgetLedger:
    def __init__(self, budget: ResearchTrialBudget = ResearchTrialBudget()):
        self.budget = budget
        self.hypotheses_created = 0
        self.trials: dict[str, int] = {}
        self.parameter_combinations: dict[str, int] = {}

    def reserve_hypothesis(self) -> None:
        if self.hypotheses_created >= self.budget.max_hypotheses:
            raise RuntimeError("hypothesis budget exhausted")
        self.hypotheses_created += 1

    def reserve_trial(self, hypothesis_id: str, *, parameter_combinations: int = 1) -> None:
        if parameter_combinations <= 0:
            raise ValueError("parameter_combinations must be positive")
        trials = self.trials.get(hypothesis_id, 0)
        combinations = self.parameter_combinations.get(hypothesis_id, 0)
        if trials + 1 > self.budget.max_trials_per_hypothesis:
            raise RuntimeError("trial budget exhausted")
        if combinations + parameter_combinations > self.budget.max_parameter_combinations:
            raise RuntimeError("parameter-combination budget exhausted")
        self.trials[hypothesis_id] = trials + 1
        self.parameter_combinations[hypothesis_id] = combinations + parameter_combinations
