from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence


@dataclass(frozen=True, slots=True)
class Fold:
    train_indices: tuple[int, ...]
    test_indices: tuple[int, ...]

    def __post_init__(self) -> None:
        if not self.train_indices or not self.test_indices:
            raise ValueError("train and test sets must both be non-empty")
        if set(self.train_indices).intersection(self.test_indices):
            raise ValueError("train/test overlap is forbidden")


@dataclass(frozen=True, slots=True)
class TemporalWindow:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.start.utcoffset() is None:
            raise ValueError("window start must be timezone-aware")
        if self.end.tzinfo is None or self.end.utcoffset() is None:
            raise ValueError("window end must be timezone-aware")
        if self.end < self.start:
            raise ValueError("window end precedes start")

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start <= end and self.end >= start


def holdout_split(n_observations: int, test_size: int, gap: int = 0) -> Fold:
    if n_observations <= 1:
        raise ValueError("at least two observations are required")
    if test_size <= 0 or gap < 0:
        raise ValueError("test_size must be positive and gap non-negative")
    test_start = n_observations - test_size
    train_end = test_start - gap
    if train_end <= 0 or test_start < 0:
        raise ValueError("insufficient observations for requested holdout")
    return Fold(tuple(range(train_end)), tuple(range(test_start, n_observations)))


def walk_forward_splits(
    n_observations: int,
    train_size: int,
    test_size: int,
    *,
    step: int | None = None,
    gap: int = 0,
    expanding: bool = False,
) -> tuple[Fold, ...]:
    if min(n_observations, train_size, test_size) <= 0:
        raise ValueError("observation, train and test sizes must be positive")
    if gap < 0:
        raise ValueError("gap must be non-negative")
    step = test_size if step is None else step
    if step <= 0:
        raise ValueError("step must be positive")

    folds: list[Fold] = []
    train_end = train_size
    while train_end + gap + test_size <= n_observations:
        train_start = 0 if expanding else train_end - train_size
        test_start = train_end + gap
        test_end = test_start + test_size
        folds.append(Fold(tuple(range(train_start, train_end)), tuple(range(test_start, test_end))))
        train_end += step
    if not folds:
        raise ValueError("requested walk-forward configuration creates no folds")
    return tuple(folds)


def purged_kfold(
    windows: Sequence[TemporalWindow],
    n_splits: int,
    *,
    embargo: int = 0,
) -> tuple[Fold, ...]:
    """Contiguous temporal K-fold with label-overlap purging and index embargo.

    A training observation is purged if its information/label window intersects
    the full test-window span. The ``embargo`` additionally removes the next N
    observations after the test block from training.
    """

    n = len(windows)
    if n_splits < 2 or n_splits > n:
        raise ValueError("n_splits must be between 2 and the number of observations")
    if embargo < 0:
        raise ValueError("embargo must be non-negative")

    base, remainder = divmod(n, n_splits)
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for fold_no in range(n_splits):
        size = base + (1 if fold_no < remainder else 0)
        bounds.append((cursor, cursor + size))
        cursor += size

    folds: list[Fold] = []
    for test_start_idx, test_end_idx in bounds:
        test_indices = tuple(range(test_start_idx, test_end_idx))
        span_start = min(windows[i].start for i in test_indices)
        span_end = max(windows[i].end for i in test_indices)
        embargo_end = min(n, test_end_idx + embargo)

        train: list[int] = []
        for i, window in enumerate(windows):
            if test_start_idx <= i < test_end_idx:
                continue
            if test_end_idx <= i < embargo_end:
                continue
            if window.overlaps(span_start, span_end):
                continue
            train.append(i)
        if not train:
            raise ValueError("purge/embargo removed all training observations")
        folds.append(Fold(tuple(train), test_indices))
    return tuple(folds)
