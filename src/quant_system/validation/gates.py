from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ValidationThresholds:
    min_dsr_probability: float = 0.95
    max_pbo: float = 0.50
    max_reality_check_p: float = 0.05
    min_walk_forward_positive_fraction: float = 0.60
    min_parameter_profitable_fraction: float = 0.60


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    passed: bool
    reasons: tuple[str, ...]


def assess_candidate(
    *,
    out_of_sample_profitable_after_costs: bool,
    walk_forward_positive_fraction: float,
    parameter_profitable_fraction: float,
    cost_stress_survived: bool,
    dsr_probability: float,
    pbo: float,
    reality_check_p: float,
    leakage_checks_passed: bool,
    thresholds: ValidationThresholds = ValidationThresholds(),
) -> ValidationDecision:
    checks = {
        "out-of-sample profitability after costs failed": out_of_sample_profitable_after_costs,
        "walk-forward stability below threshold": walk_forward_positive_fraction >= thresholds.min_walk_forward_positive_fraction,
        "parameter robustness below threshold": parameter_profitable_fraction >= thresholds.min_parameter_profitable_fraction,
        "cost stress failed": cost_stress_survived,
        "deflated Sharpe confidence below threshold": dsr_probability >= thresholds.min_dsr_probability,
        "PBO exceeds threshold": pbo <= thresholds.max_pbo,
        "reality-check p-value exceeds threshold": reality_check_p <= thresholds.max_reality_check_p,
        "leakage checks failed": leakage_checks_passed,
    }
    reasons = tuple(reason for reason, passed in checks.items() if not passed)
    return ValidationDecision(not reasons, reasons)
