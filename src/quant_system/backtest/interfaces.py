from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ExecutionAssumptions:
    commission_bps: Decimal
    spread_bps: Decimal
    slippage_bps: Decimal
    impact_bps: Decimal
    financing_bps_annual: Decimal
    borrow_bps_annual: Decimal
    latency_ms: int
    partial_fill_fraction: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        for name in (
            "commission_bps", "spread_bps", "slippage_bps", "impact_bps",
            "financing_bps_annual", "borrow_bps_annual",
        ):
            if getattr(self, name) < Decimal("0"):
                raise ValueError(f"{name} must be non-negative")
        if not Decimal("0") <= self.partial_fill_fraction <= Decimal("1"):
            raise ValueError("partial_fill_fraction must be in [0,1]")
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be >= 0")


class StrategyModel(Protocol):
    def on_event(self, event: object) -> object | None: ...


class BacktestEngine(Protocol):
    def run(self, strategy: StrategyModel, assumptions: ExecutionAssumptions) -> object: ...
