from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import FrozenSet
from uuid import UUID, uuid4

from quant_system.core.enums import StrategyState


@dataclass(frozen=True, slots=True)
class StrategySpec:
    strategy_id: UUID
    name: str
    hypothesis: str
    rationale: str
    asset_universe: tuple[str, ...]
    timeframe: str
    data_sources: tuple[str, ...]
    features: tuple[str, ...]
    entry_rules: str
    exit_rules: str
    position_sizing: str
    expected_holding_period: str
    expected_transaction_cost_bps: float
    expected_capacity_usd: float | None
    risks: tuple[str, ...]
    regime_dependencies: tuple[str, ...]
    code_version: str

    @staticmethod
    def new(**kwargs) -> "StrategySpec":
        return StrategySpec(strategy_id=uuid4(), **kwargs)


_ALLOWED_TRANSITIONS: dict[StrategyState, FrozenSet[StrategyState]] = {
    StrategyState.PROPOSED: frozenset({StrategyState.EXPERIMENTAL, StrategyState.REJECTED}),
    StrategyState.EXPERIMENTAL: frozenset({StrategyState.REJECTED, StrategyState.OOS_VALIDATED, StrategyState.RESEARCH}),
    StrategyState.OOS_VALIDATED: frozenset({StrategyState.PAPER, StrategyState.REJECTED, StrategyState.RESEARCH}),
    StrategyState.PAPER: frozenset({StrategyState.SHADOW, StrategyState.REJECTED, StrategyState.RESEARCH, StrategyState.PAUSED}),
    StrategyState.SHADOW: frozenset({StrategyState.LIVE_1, StrategyState.RESEARCH, StrategyState.PAUSED, StrategyState.REJECTED}),
    StrategyState.LIVE_1: frozenset({StrategyState.LIVE_2, StrategyState.REDUCED, StrategyState.WATCH, StrategyState.PAUSED, StrategyState.RESEARCH}),
    StrategyState.LIVE_2: frozenset({StrategyState.LIVE_3, StrategyState.REDUCED, StrategyState.WATCH, StrategyState.PAUSED, StrategyState.RESEARCH}),
    StrategyState.LIVE_3: frozenset({StrategyState.LIVE_4, StrategyState.REDUCED, StrategyState.WATCH, StrategyState.PAUSED, StrategyState.RESEARCH}),
    StrategyState.LIVE_4: frozenset({StrategyState.REDUCED, StrategyState.WATCH, StrategyState.PAUSED, StrategyState.RESEARCH}),
    StrategyState.WATCH: frozenset({StrategyState.REDUCED, StrategyState.PAUSED, StrategyState.RESEARCH, StrategyState.LIVE_1, StrategyState.LIVE_2, StrategyState.LIVE_3, StrategyState.LIVE_4}),
    StrategyState.REDUCED: frozenset({StrategyState.WATCH, StrategyState.PAUSED, StrategyState.RESEARCH, StrategyState.LIVE_1, StrategyState.LIVE_2, StrategyState.LIVE_3}),
    StrategyState.PAUSED: frozenset({StrategyState.RESEARCH, StrategyState.RETIRED, StrategyState.PAPER, StrategyState.SHADOW}),
    StrategyState.RESEARCH: frozenset({StrategyState.EXPERIMENTAL, StrategyState.REJECTED, StrategyState.RETIRED}),
    StrategyState.REJECTED: frozenset({StrategyState.RESEARCH, StrategyState.RETIRED}),
    StrategyState.RETIRED: frozenset(),
}


@dataclass(slots=True)
class StrategyRecord:
    spec: StrategySpec
    state: StrategyState = StrategyState.PROPOSED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def transition(self, target: StrategyState) -> None:
        if target == self.state:
            return
        if target not in _ALLOWED_TRANSITIONS[self.state]:
            raise ValueError(f"illegal strategy transition: {self.state} -> {target}")
        self.state = target
        self.updated_at = datetime.now(timezone.utc)
