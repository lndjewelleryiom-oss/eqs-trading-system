from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CandidatePriority:
    hypothesis_id: str
    evidence_strength: float
    novelty: float
    diversification: float
    implementation_readiness: float
    score: float


def prioritize(
    candidates: list[tuple[str, float, float, float, float]],
) -> tuple[CandidatePriority, ...]:
    """Rank by evidence first, with novelty/diversification as secondary terms.

    Input tuple: id, evidence_strength, novelty, diversification, implementation_readiness.
    No return metric appears here by design.
    """
    result = []
    for hypothesis_id, evidence, novelty, diversification, readiness in candidates:
        if any(not 0 <= value <= 1 for value in (evidence, novelty, diversification, readiness)):
            raise ValueError("priority dimensions must be in [0, 1]")
        score = 0.55 * evidence + 0.15 * novelty + 0.20 * diversification + 0.10 * readiness
        result.append(CandidatePriority(hypothesis_id, evidence, novelty, diversification, readiness, score))
    return tuple(sorted(result, key=lambda item: (-item.score, item.hypothesis_id)))
