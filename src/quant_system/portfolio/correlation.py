from __future__ import annotations

from math import sqrt
from statistics import fmean


def pearson_correlation(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or len(left) < 2:
        raise ValueError("return series must have equal length >= 2")
    left_mean = fmean(left)
    right_mean = fmean(right)
    covariance = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_var = sum((a - left_mean) ** 2 for a in left)
    right_var = sum((b - right_mean) ** 2 for b in right)
    denominator = sqrt(left_var * right_var)
    if denominator == 0:
        return 0.0
    return max(-1.0, min(1.0, covariance / denominator))


def correlation_matrix(returns: dict[str, list[float]]) -> dict[tuple[str, str], float]:
    keys = sorted(returns)
    result: dict[tuple[str, str], float] = {}
    for i, left in enumerate(keys):
        for right in keys[i + 1 :]:
            result[(left, right)] = pearson_correlation(returns[left], returns[right])
    return result
