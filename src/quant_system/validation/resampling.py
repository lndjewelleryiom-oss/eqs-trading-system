from __future__ import annotations

from dataclasses import dataclass
import math
import random
from statistics import mean
from typing import Callable, Sequence


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    estimate: float
    lower: float
    upper: float
    confidence: float
    simulations: int


@dataclass(frozen=True, slots=True)
class MonteCarloResult:
    median_terminal_equity: float
    p05_terminal_equity: float
    p95_terminal_equity: float
    median_max_drawdown: float
    p95_max_drawdown: float
    simulations: int


def _quantile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("values cannot be empty")
    if not 0 <= probability <= 1:
        raise ValueError("probability must be in [0, 1]")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def circular_block_sample(values: Sequence[float], rng: random.Random, block_size: int) -> list[float]:
    if not values:
        raise ValueError("values cannot be empty")
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    n = len(values)
    sampled: list[float] = []
    while len(sampled) < n:
        start = rng.randrange(n)
        sampled.extend(values[(start + offset) % n] for offset in range(block_size))
    return sampled[:n]


def bootstrap_confidence_interval(
    values: Sequence[float],
    *,
    statistic: Callable[[Sequence[float]], float] = mean,
    simulations: int = 2000,
    confidence: float = 0.95,
    seed: int = 0,
    block_size: int = 1,
) -> BootstrapResult:
    if not values:
        raise ValueError("values cannot be empty")
    if simulations <= 0:
        raise ValueError("simulations must be positive")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be between 0 and 1")

    rng = random.Random(seed)
    distribution = [
        float(statistic(circular_block_sample(values, rng, block_size)))
        for _ in range(simulations)
    ]
    alpha = (1 - confidence) / 2
    return BootstrapResult(
        estimate=float(statistic(values)),
        lower=_quantile(distribution, alpha),
        upper=_quantile(distribution, 1 - alpha),
        confidence=confidence,
        simulations=simulations,
    )


def _max_drawdown(path: Sequence[float]) -> float:
    peak = path[0]
    worst = 0.0
    for value in path:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def monte_carlo_trade_paths(
    trade_returns: Sequence[float],
    *,
    initial_equity: float = 1.0,
    simulations: int = 2000,
    seed: int = 0,
) -> MonteCarloResult:
    if not trade_returns:
        raise ValueError("trade_returns cannot be empty")
    if initial_equity <= 0 or simulations <= 0:
        raise ValueError("initial_equity and simulations must be positive")
    if any(r <= -1 for r in trade_returns):
        raise ValueError("returns <= -100% are invalid for multiplicative paths")

    rng = random.Random(seed)
    terminal: list[float] = []
    drawdowns: list[float] = []
    n = len(trade_returns)
    for _ in range(simulations):
        equity = initial_equity
        path = [equity]
        for _ in range(n):
            equity *= 1 + float(trade_returns[rng.randrange(n)])
            path.append(equity)
        terminal.append(equity)
        drawdowns.append(_max_drawdown(path))

    return MonteCarloResult(
        median_terminal_equity=_quantile(terminal, 0.5),
        p05_terminal_equity=_quantile(terminal, 0.05),
        p95_terminal_equity=_quantile(terminal, 0.95),
        median_max_drawdown=_quantile(drawdowns, 0.5),
        p95_max_drawdown=_quantile(drawdowns, 0.95),
        simulations=simulations,
    )
