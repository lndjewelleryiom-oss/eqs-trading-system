from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Iterable
from uuid import UUID

from quant_system.core.enums import StrategyState
from quant_system.registry.models import StrategyRecord, StrategySpec


class StrategyRegistry:
    """Canonical in-process registry interface for the foundation phase.

    Production persistence is backed by the PostgreSQL schema. This class keeps
    lifecycle semantics testable before the database adapter is introduced.
    """

    def __init__(self) -> None:
        self._records: dict[UUID, StrategyRecord] = {}
        self._history: list[dict[str, object]] = []

    def register(self, spec: StrategySpec) -> StrategyRecord:
        if spec.strategy_id in self._records:
            raise ValueError(f"strategy already exists: {spec.strategy_id}")
        record = StrategyRecord(spec=spec)
        self._records[spec.strategy_id] = record
        self._history.append({
            "strategy_id": spec.strategy_id,
            "from_state": None,
            "to_state": StrategyState.PROPOSED,
            "reason": "REGISTERED",
            "changed_at": datetime.now(timezone.utc),
        })
        return record

    def get(self, strategy_id: UUID) -> StrategyRecord:
        try:
            return self._records[strategy_id]
        except KeyError as exc:
            raise KeyError(f"unknown strategy: {strategy_id}") from exc

    def transition(self, strategy_id: UUID, target: StrategyState, *, reason: str) -> StrategyRecord:
        if not reason.strip():
            raise ValueError("strategy transition requires a reason")
        record = self.get(strategy_id)
        previous = record.state
        record.transition(target)
        if previous != target:
            self._history.append({
                "strategy_id": strategy_id,
                "from_state": previous,
                "to_state": target,
                "reason": reason,
                "changed_at": record.updated_at,
            })
        return record

    def history(self, strategy_id: UUID) -> tuple[dict[str, object], ...]:
        return tuple(h.copy() for h in self._history if h["strategy_id"] == strategy_id)

    def list_by_state(self, states: Iterable[StrategyState]) -> tuple[StrategyRecord, ...]:
        wanted = frozenset(states)
        return tuple(r for r in self._records.values() if r.state in wanted)
