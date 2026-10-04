from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from quant_system.portfolio import (
    AllocationResult,
    ConstrainedPortfolioAllocator,
    StrategyAllocationInput,
)
from quant_system.regime.models import MarketRegime

from .lifecycle import PersistentStrategyLifecycle, StrategyLifecycleState


@dataclass(frozen=True, slots=True)
class PaperAllocationCandidate:
    strategy_id: str
    strategy_version: str
    researched_base_weight: float
    confidence: float
    expected_volatility: float
    drawdown_fraction: float
    liquidity_score: float
    capacity_notional: Decimal
    regime_fit: dict[MarketRegime, float]
    degradation_multiplier: float = 1.0


@dataclass(frozen=True, slots=True)
class AutonomousPaperAllocation:
    result: AllocationResult
    lifecycle_states: dict[str, str]
    enforced_caps: dict[str, float]
    excluded_strategies: dict[str, str]


class AutonomousPaperAllocator:
    """Lifecycle-aware conservative PAPER allocator.

    It can only reduce researched risk. It never grants LIVE authority and never
    increases a strategy above its researched base weight.
    """

    def __init__(
        self,
        allocator: ConstrainedPortfolioAllocator | None = None,
        *,
        canary_weight_cap: float = 0.02,
        reduced_state_multiplier: float = 0.40,
    ):
        self.allocator = allocator or ConstrainedPortfolioAllocator()
        self.canary_weight_cap = float(canary_weight_cap)
        self.reduced_state_multiplier = float(reduced_state_multiplier)
        if not 0 < self.canary_weight_cap <= 0.05:
            raise ValueError("canary_weight_cap must be in (0,0.05]")
        if not 0 <= self.reduced_state_multiplier <= 1:
            raise ValueError("reduced_state_multiplier must be in [0,1]")

    def allocate(
        self,
        *,
        lifecycle: PersistentStrategyLifecycle,
        candidates: list[PaperAllocationCandidate],
        regime: MarketRegime,
        regime_confidence: float,
        nav: Decimal,
        correlations: dict[tuple[str, str], float] | None = None,
    ) -> AutonomousPaperAllocation:
        if len({c.strategy_id for c in candidates}) != len(candidates):
            raise ValueError("strategy_id values must be unique")

        allocation_inputs: list[StrategyAllocationInput] = []
        states: dict[str, str] = {}
        caps: dict[str, float] = {}
        excluded: dict[str, str] = {}

        for candidate in candidates:
            record = lifecycle.get(candidate.strategy_id, candidate.strategy_version)
            states[candidate.strategy_id] = record.state.value

            if record.state in {
                StrategyLifecycleState.RESEARCH,
                StrategyLifecycleState.VALIDATED,
                StrategyLifecycleState.PAUSED,
                StrategyLifecycleState.RETIRED,
            }:
                excluded[candidate.strategy_id] = f"LIFECYCLE_{record.state.value}"
                continue

            base_weight = candidate.researched_base_weight
            degradation = candidate.degradation_multiplier
            if record.state is StrategyLifecycleState.PAPER_CANARY:
                base_weight = min(base_weight, self.canary_weight_cap)
                caps[candidate.strategy_id] = self.canary_weight_cap
            elif record.state is StrategyLifecycleState.REDUCED:
                degradation = min(degradation, self.reduced_state_multiplier)
                caps[candidate.strategy_id] = float(
                    Decimal(str(candidate.researched_base_weight))
                    * Decimal(str(self.reduced_state_multiplier))
                )

            allocation_inputs.append(
                StrategyAllocationInput(
                    strategy_id=candidate.strategy_id,
                    base_weight=base_weight,
                    confidence=candidate.confidence,
                    expected_volatility=candidate.expected_volatility,
                    drawdown_fraction=candidate.drawdown_fraction,
                    liquidity_score=candidate.liquidity_score,
                    capacity_notional=candidate.capacity_notional,
                    regime_fit=candidate.regime_fit,
                    degradation_multiplier=degradation,
                )
            )

        result = self.allocator.allocate(
            allocation_inputs,
            regime=regime,
            regime_confidence=regime_confidence,
            nav=nav,
            correlations=correlations,
        )
        weights = dict(result.weights)
        reasons = {k: tuple(v) for k, v in result.reasons.items()}
        for strategy_id, cap in caps.items():
            if strategy_id in weights and weights[strategy_id] > cap:
                weights[strategy_id] = cap
                reasons[strategy_id] = tuple(reasons.get(strategy_id, ())) + ("LIFECYCLE_RISK_CAP",)
        for strategy_id, reason in excluded.items():
            weights[strategy_id] = 0.0
            reasons[strategy_id] = (reason,)

        normalized = AllocationResult(
            weights=dict(sorted(weights.items())),
            gross_weight=sum(weights.values()),
            cash_weight=max(0.0, 1.0 - sum(weights.values())),
            reasons=dict(sorted(reasons.items())),
        )
        return AutonomousPaperAllocation(
            result=normalized,
            lifecycle_states=dict(sorted(states.items())),
            enforced_caps=dict(sorted(caps.items())),
            excluded_strategies=dict(sorted(excluded.items())),
        )
