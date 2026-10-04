from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import json
from typing import Any, Mapping, Protocol


@dataclass(frozen=True, slots=True)
class InterfaceMeta:
    schema_version: str
    generated_at: str
    access_mode: str
    adapter: str
    data_classification: str
    mutations_enabled: bool


@dataclass(frozen=True, slots=True)
class EqsInterfaceSnapshot:
    meta: InterfaceMeta
    command: Mapping[str, Any]
    data: Mapping[str, Any]
    alpha_research: Mapping[str, Any]
    strategies: Mapping[str, Any]
    risk: Mapping[str, Any]
    execution: Mapping[str, Any]
    evidence: Mapping[str, Any]
    system_health: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_payload(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class ReadOnlyEqsAdapter(Protocol):
    """One-way interface seam. Implementations expose state only, never commands."""

    def snapshot(self, *, now: datetime | None = None) -> EqsInterfaceSnapshot:
        ...
