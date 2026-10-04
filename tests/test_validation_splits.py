from datetime import datetime, timedelta, timezone

import pytest

from quant_system.validation.evaluation import evaluate_fold, evaluate_walk_forward
from quant_system.validation.splits import TemporalWindow, holdout_split, purged_kfold, walk_forward_splits


def test_holdout_respects_gap_and_future_isolation():
    fold = holdout_split(20, test_size=5, gap=2)
    assert fold.train_indices == tuple(range(13))
    assert fold.test_indices == tuple(range(15, 20))
    assert max(fold.train_indices) < min(fold.test_indices)


def test_walk_forward_rolling_and_expanding_are_temporally_ordered():
    rolling = walk_forward_splits(30, 10, 5, step=5, gap=1)
    expanding = walk_forward_splits(30, 10, 5, step=5, gap=1, expanding=True)
    assert len(rolling) == len(expanding) == 3
    assert all(max(f.train_indices) < min(f.test_indices) for f in rolling)
    assert len(rolling[-1].train_indices) == 10
    assert len(expanding[-1].train_indices) == 20


def test_evaluator_refits_only_on_each_training_window():
    data = list(range(30))
    folds = walk_forward_splits(30, 10, 5, step=5)
    seen_train_maxima = []

    def fit(train):
        seen_train_maxima.append(max(train))
        return sum(train) / len(train)

    def score(model, sample):
        return sum(x - model for x in sample) / len(sample)

    result = evaluate_walk_forward(data, folds, fit=fit, score=score)
    assert seen_train_maxima == [9, 14, 19, 24]
    assert len(result.folds) == 4
    assert result.positive_test_fraction == 1.0


def test_evaluator_rejects_non_temporal_fold():
    from quant_system.validation.splits import Fold

    with pytest.raises(ValueError, match="training rows to precede"):
        evaluate_fold([1, 2, 3], Fold((0, 2), (1,)), fit=lambda x: 0, score=lambda m, x: 0)


def test_purged_kfold_removes_overlapping_labels_and_embargo_rows():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    windows = [TemporalWindow(start + timedelta(days=i), start + timedelta(days=i + 2)) for i in range(12)]
    folds = purged_kfold(windows, 3, embargo=1)

    first = folds[0]
    assert first.test_indices == (0, 1, 2, 3)
    # test span is day 0 through day 5, so rows 4-5 overlap; row 4 is also embargoed.
    assert 4 not in first.train_indices
    assert 5 not in first.train_indices
    assert 6 in first.train_indices

    for fold in folds:
        test_start = min(windows[i].start for i in fold.test_indices)
        test_end = max(windows[i].end for i in fold.test_indices)
        assert all(not windows[i].overlaps(test_start, test_end) for i in fold.train_indices)
