"""Purged walk-forward splits (app/research/cv.py): the guarantees that make
a reported AUC survive review."""
from __future__ import annotations

from datetime import datetime, timedelta

from app.research.cv import oof_predictions, purged_walk_forward


def _times(n=3000, start=datetime(2025, 1, 1), step=timedelta(hours=2)):
    # Shuffled on purpose: callers need not pre-sort.
    ts = [start + i * step for i in range(n)]
    return ts[::2] + ts[1::2]


def test_every_training_row_precedes_its_block_by_the_purge():
    times = _times()
    purge = timedelta(days=2)
    folds = list(purged_walk_forward(times, purge=purge, min_train=200, min_valid=10))
    assert len(folds) >= 5
    for f in folds:
        first_valid = min(times[i] for i in f.valid)
        assert max(times[i] for i in f.train) < first_valid - purge
        assert {times[i].strftime("%Y-%m") for i in f.valid} == {f.label}
        assert not set(f.train) & set(f.valid)


def test_windows_expand_and_early_months_are_skipped():
    times = _times()
    folds = list(purged_walk_forward(times, min_train=500, min_valid=10))
    sizes = [len(f.train) for f in folds]
    assert sizes == sorted(sizes) and sizes[0] >= 500
    assert folds[0].label != "2025-01"


def test_oof_never_scores_a_row_with_a_model_that_saw_it():
    times = _times(1500)
    seen_by: dict[int, set[int]] = {}

    def fit_predict(train, valid):
        for i in valid:
            seen_by[i] = set(train)
        return [0.5] * len(valid)

    oof = oof_predictions(times, fit_predict, purge=timedelta(days=1), min_train=100)
    scored = [i for i, p in enumerate(oof) if p is not None]
    assert scored and len(scored) < len(times)       # the first month has no history
    for i in scored:
        assert i not in seen_by[i]
        assert all(times[j] < times[i] for j in seen_by[i])
