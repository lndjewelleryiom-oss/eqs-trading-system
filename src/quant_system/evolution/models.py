from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
from uuid import UUID, uuid4


class HypothesisStatus(StrEnum):
    PROPOSED = "PROPOSED"
    QUEUED = "QUEUED"
    TESTING = "TESTING"
    REJECTED = "REJECTED"
    CANDIDATE = "CANDIDATE"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class AcceptanceCriteria:
    min_oos_mean: float = 0.0
    min_walk_forward_positive_fraction: float = 0.60
    min_parameter_robust_fraction: float = 0.60
    min_dsr_probability: float = 0.95
    max_pbo: float = 0.50
    max_reality_check_p: float = 0.05

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "max_pbo": self.max_pbo,
                "max_reality_check_p": self.max_reality_check_p,
                "min_dsr_probability": self.min_dsr_probability,
                "min_oos_mean": self.min_oos_mean,
                "min_parameter_robust_fraction": self.min_parameter_robust_fraction,
                "min_walk_forward_positive_fraction": self.min_walk_forward_positive_fraction,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(payload.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Hypothesis:
    statement: str
    economic_rationale: str
    family: str
    asset_class: str
    horizon: str
    source_anomaly: str
    criteria: AcceptanceCriteria = field(default_factory=AcceptanceCriteria)
    hypothesis_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        for field_name in ("statement", "economic_rationale", "family", "asset_class", "horizon", "source_anomaly"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"{field_name} must not be empty")


@dataclass(frozen=True, slots=True)
class ResearchEvidence:
    oos_mean_after_costs: float
    walk_forward_positive_fraction: float
    parameter_robust_fraction: float
    dsr_probability: float
    pbo: float
    reality_check_p: float
    leakage_checks_passed: bool
    cost_stress_passed: bool
    evidence_count: int


@dataclass(frozen=True, slots=True)
class HypothesisRecord:
    hypothesis: Hypothesis
    status: HypothesisStatus
    criteria_fingerprint: str
    evidence: ResearchEvidence | None = None
    reasons: tuple[str, ...] = ()
