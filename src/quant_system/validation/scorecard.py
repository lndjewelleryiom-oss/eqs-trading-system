from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import mean, median, stdev
from typing import Sequence

from .statistics import _sample_kurtosis_pearson, _sample_skewness


@dataclass(frozen=True, slots=True)
class StrategyScorecard:
    total_return: float
    cagr: float
    average_return: float
    median_return: float
    sharpe: float
    sortino: float
    calmar: float
    max_drawdown: float
    max_drawdown_duration: int
    profit_factor: float
    win_rate: float
    expectancy: float
    average_win: float
    average_loss: float
    payoff_ratio: float
    trade_count: int
    tail_loss_p05: float
    var_95: float
    expected_shortfall_95: float
    skew: float
    kurtosis: float
    turnover: float | None = None
    exposure: float | None = None
    capacity: float | None = None
    cost_sensitivity: float | None = None

    def as_dict(self) -> dict[str, float | int | None]:
        return asdict(self)


def _quantile(values: Sequence[float], p: float) -> float:
    ordered = sorted(values)
    pos = (len(ordered) - 1) * p
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    w = pos - lo
    return ordered[lo] * (1 - w) + ordered[hi] * w


def _drawdown_stats(returns: Sequence[float]) -> tuple[float, int]:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    duration = 0
    worst_duration = 0
    for r in returns:
        equity *= 1 + r
        if equity >= peak:
            peak = equity
            duration = 0
        else:
            duration += 1
            worst_duration = max(worst_duration, duration)
            worst = max(worst, (peak - equity) / peak)
    return worst, worst_duration


def build_scorecard(
    returns: Sequence[float],
    *,
    periods_per_year: float = 252.0,
    turnover: float | None = None,
    exposure: float | None = None,
    capacity: float | None = None,
    cost_sensitivity: float | None = None,
) -> StrategyScorecard:
    if len(returns) < 2:
        raise ValueError("at least two returns are required")
    if periods_per_year <= 0:
        raise ValueError("periods_per_year must be positive")
    if any(r <= -1 for r in returns):
        raise ValueError("returns <= -100% are invalid for compounded scorecards")

    values = [float(x) for x in returns]
    equity = math.prod(1 + x for x in values)
    total_return = equity - 1
    years = len(values) / periods_per_year
    cagr = equity ** (1 / years) - 1 if years > 0 else 0.0
    avg = mean(values)
    med = median(values)
    sigma = stdev(values)
    sharpe = avg / sigma * math.sqrt(periods_per_year) if sigma else (math.inf if avg > 0 else 0.0)
    downside = [min(0.0, x) for x in values]
    downside_dev = math.sqrt(mean([x * x for x in downside]))
    sortino = avg / downside_dev * math.sqrt(periods_per_year) if downside_dev else math.inf
    max_dd, dd_duration = _drawdown_stats(values)
    calmar = cagr / max_dd if max_dd else math.inf

    wins = [x for x in values if x > 0]
    losses = [x for x in values if x < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss else math.inf
    average_win = mean(wins) if wins else 0.0
    average_loss = mean(losses) if losses else 0.0
    payoff = average_win / abs(average_loss) if average_loss else math.inf
    q05 = _quantile(values, 0.05)
    tail = [x for x in values if x <= q05]

    return StrategyScorecard(
        total_return=total_return,
        cagr=cagr,
        average_return=avg,
        median_return=med,
        sharpe=sharpe,
        sortino=sortino,
        calmar=calmar,
        max_drawdown=max_dd,
        max_drawdown_duration=dd_duration,
        profit_factor=profit_factor,
        win_rate=len(wins) / len(values),
        expectancy=avg,
        average_win=average_win,
        average_loss=average_loss,
        payoff_ratio=payoff,
        trade_count=len(values),
        tail_loss_p05=q05,
        var_95=max(0.0, -q05),
        expected_shortfall_95=max(0.0, -mean(tail)),
        skew=_sample_skewness(values),
        kurtosis=_sample_kurtosis_pearson(values),
        turnover=turnover,
        exposure=exposure,
        capacity=capacity,
        cost_sensitivity=cost_sensitivity,
    )
