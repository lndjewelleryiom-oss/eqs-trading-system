from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

from .splits import Fold


T = TypeVar("T")
M = TypeVar("M")


@dataclass(frozen=True, slots=True)
class FoldEvaluation:
    fold_number: int
    train_score: float
    test_score: float
    train_size: int
    test_size: int


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluation:
    folds: tuple[FoldEvaluation, ...]

    @property
    def positive_test_fraction(self) -> float:
        return sum(fold.test_score > 0 for fold in self.folds) / len(self.folds)

    @property
    def mean_test_score(self) -> float:
        return sum(fold.test_score for fold in self.folds) / len(self.folds)


def evaluate_fold(
    data: Sequence[T],
    fold: Fold,
    *,
    fit: Callable[[Sequence[T]], M],
    score: Callable[[M, Sequence[T]], float],
    fold_number: int = 0,
) -> FoldEvaluation:
    if max(fold.train_indices) >= min(fold.test_indices):
        raise ValueError("temporal evaluation requires all training rows to precede test rows")
    if max((*fold.train_indices, *fold.test_indices)) >= len(data):
        raise IndexError("fold index exceeds data length")
    train = [data[i] for i in fold.train_indices]
    test = [data[i] for i in fold.test_indices]
    model = fit(train)
    return FoldEvaluation(
        fold_number=fold_number,
        train_score=float(score(model, train)),
        test_score=float(score(model, test)),
        train_size=len(train),
        test_size=len(test),
    )


def evaluate_walk_forward(
    data: Sequence[T],
    folds: Sequence[Fold],
    *,
    fit: Callable[[Sequence[T]], M],
    score: Callable[[M, Sequence[T]], float],
) -> WalkForwardEvaluation:
    if not folds:
        raise ValueError("at least one fold is required")
    results = tuple(
        evaluate_fold(data, fold, fit=fit, score=score, fold_number=i)
        for i, fold in enumerate(folds)
    )
    return WalkForwardEvaluation(results)
