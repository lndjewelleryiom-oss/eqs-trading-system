from __future__ import annotations

from dataclasses import dataclass

from .models import AcceptanceCriteria, ResearchEvidence


@dataclass(frozen=True, slots=True)
class FalsificationDecision:
    passed: bool
    reasons: tuple[str, ...]


def falsify(evidence: ResearchEvidence, criteria: AcceptanceCriteria) -> FalsificationDecision:
    checks = {
        "OOS_MEAN_FAILED": evidence.oos_mean_after_costs > criteria.min_oos_mean,
        "WALK_FORWARD_FAILED": evidence.walk_forward_positive_fraction >= criteria.min_walk_forward_positive_fraction,
        "PARAMETER_ROBUSTNESS_FAILED": evidence.parameter_robust_fraction >= criteria.min_parameter_robust_fraction,
        "DSR_FAILED": evidence.dsr_probability >= criteria.min_dsr_probability,
        "PBO_FAILED": evidence.pbo <= criteria.max_pbo,
        "REALITY_CHECK_FAILED": evidence.reality_check_p <= criteria.max_reality_check_p,
        "LEAKAGE_CHECK_FAILED": evidence.leakage_checks_passed,
        "COST_STRESS_FAILED": evidence.cost_stress_passed,
        "INSUFFICIENT_EVIDENCE": evidence.evidence_count > 0,
    }
    reasons = tuple(name for name, passed in checks.items() if not passed)
    return FalsificationDecision(not reasons, reasons or ("ALL_PRECOMMITTED_CHECKS_PASSED",))
