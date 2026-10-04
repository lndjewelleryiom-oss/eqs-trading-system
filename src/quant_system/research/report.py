from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from quant_system.validation.scorecard import StrategyScorecard
from quant_system.validation.statistics import DeflatedSharpeResult, PBOResult, RealityCheckResult


@dataclass(frozen=True, slots=True)
class ResearchReport:
    strategy_id: str
    hypothesis: str
    stage: str
    scorecard: StrategyScorecard
    deflated_sharpe: DeflatedSharpeResult
    pbo: PBOResult
    reality_check: RealityCheckResult
    weaknesses: Sequence[str]
    failure_conditions: Sequence[str]

    def to_markdown(self) -> str:
        s = self.scorecard
        lines = [
            f"# Research Report — {self.strategy_id}",
            "",
            f"**Stage:** {self.stage}",
            f"**Hypothesis:** {self.hypothesis}",
            "",
            "## Scorecard",
            f"- Total return: {s.total_return:.6f}",
            f"- CAGR: {s.cagr:.6f}",
            f"- Sharpe: {s.sharpe:.6f}",
            f"- Sortino: {s.sortino:.6f}",
            f"- Maximum drawdown: {s.max_drawdown:.6f}",
            f"- Profit factor: {s.profit_factor:.6f}",
            f"- Trade count: {s.trade_count}",
            "",
            "## Overfitting defence",
            f"- Deflated Sharpe probability: {self.deflated_sharpe.probability:.6f}",
            f"- PBO: {self.pbo.probability_of_backtest_overfitting:.6f}",
            f"- Reality-check p-value: {self.reality_check.p_value:.6f}",
            "",
            "## Weaknesses",
            *[f"- {item}" for item in self.weaknesses],
            "",
            "## Failure conditions",
            *[f"- {item}" for item in self.failure_conditions],
            "",
        ]
        return "\n".join(lines)
