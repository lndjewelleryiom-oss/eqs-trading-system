from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    minimum_trades_met: bool
    out_of_sample_profitable_after_costs: bool
    walk_forward_stable: bool
    parameter_robust: bool
    cost_stress_survived: bool
    leakage_checks_passed: bool
    multiple_testing_adjusted: bool
    paper_execution_acceptable: bool = False

    def research_gate_passed(self) -> bool:
        return all((
            self.minimum_trades_met,
            self.out_of_sample_profitable_after_costs,
            self.walk_forward_stable,
            self.parameter_robust,
            self.cost_stress_survived,
            self.leakage_checks_passed,
            self.multiple_testing_adjusted,
        ))
