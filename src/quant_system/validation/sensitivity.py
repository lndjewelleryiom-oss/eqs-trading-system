from __future__ import annotations

from dataclasses import dataclass
from itertools import product
from typing import Callable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class SurfacePoint:
    inputs: tuple[tuple[str, float], ...]
    value: float

    @property
    def mapping(self) -> dict[str, float]:
        return dict(self.inputs)


def parameter_surface(
    grid: Mapping[str, Sequence[float]],
    evaluator: Callable[[Mapping[str, float]], float],
) -> tuple[SurfacePoint, ...]:
    if not grid:
        raise ValueError("parameter grid cannot be empty")
    names = tuple(sorted(grid))
    values = []
    for name in names:
        axis = tuple(grid[name])
        if not axis:
            raise ValueError(f"parameter axis {name!r} is empty")
        values.append(axis)

    points: list[SurfacePoint] = []
    for combination in product(*values):
        params = dict(zip(names, combination, strict=True))
        score = float(evaluator(params))
        points.append(SurfacePoint(tuple((name, float(params[name])) for name in names), score))
    return tuple(points)


def cost_surface(
    base_costs: Mapping[str, float],
    multipliers: Sequence[float],
    evaluator: Callable[[Mapping[str, float]], float],
) -> tuple[SurfacePoint, ...]:
    if not base_costs:
        raise ValueError("base costs cannot be empty")
    if not multipliers:
        raise ValueError("cost multipliers cannot be empty")
    names = tuple(sorted(base_costs))
    points: list[SurfacePoint] = []
    for multiplier in multipliers:
        if multiplier < 0:
            raise ValueError("cost multipliers must be non-negative")
        costs = {name: float(base_costs[name]) * float(multiplier) for name in names}
        encoded = tuple((f"{name}@x{float(multiplier):g}", costs[name]) for name in names)
        points.append(SurfacePoint(encoded, float(evaluator(costs))))
    return tuple(points)


def profitable_fraction(points: Sequence[SurfacePoint], threshold: float = 0.0) -> float:
    if not points:
        raise ValueError("surface cannot be empty")
    return sum(point.value > threshold for point in points) / len(points)
