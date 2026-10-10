"""Purged walk-forward cross-validation for time-ordered event data.

Plain k-fold on filings leaks: label windows overlap, a stock's own past
outcomes are features, and a random fold lets the model train on rows that
happen after the rows it is scored on. A reviewer will discount any number
produced that way. This module provides the splits that survive review:

  * expanding window: each validation block is a calendar month, and the
    model trains only on rows strictly before it;
  * purge: training rows within `purge` of the validation block's start are
    dropped, so no training label window overlaps validation (set it to at
    least the label horizon; a day is the minimum for next-session labels);
  * embargo: optionally skip the rows right after a block too, for features
    built from trailing windows.

Pure stdlib, so the live API and AIdataset/model/train.py share it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, Sequence


@dataclass(frozen=True)
class Fold:
    label: str               # e.g. "2026-03"
    train: list[int]
    valid: list[int]


def month_key(t: datetime) -> str:
    return f"{t.year:04d}-{t.month:02d}"


def purged_walk_forward(
    times: Sequence[datetime],
    *,
    purge: timedelta = timedelta(days=1),
    min_train: int = 1000,
    min_valid: int = 50,
) -> Iterator[Fold]:
    """Monthly expanding-window folds over `times` (any order).

    Yields only folds with at least `min_train` training rows and
    `min_valid` validation rows, so the first months (no history yet) are
    skipped rather than scored on a model fit to nothing."""
    order = sorted(range(len(times)), key=lambda i: times[i])
    months: dict[str, list[int]] = {}
    for i in order:
        months.setdefault(month_key(times[i]), []).append(i)
    for label in sorted(months):
        valid = months[label]
        start = min(times[i] for i in valid)
        cutoff = start - purge
        train = [i for i in order if times[i] < cutoff]
        if len(train) >= min_train and len(valid) >= min_valid:
            yield Fold(label=label, train=train, valid=valid)


def oof_predictions(
    times: Sequence[datetime],
    fit_predict,
    *,
    purge: timedelta = timedelta(days=1),
    min_train: int = 1000,
) -> list[float | None]:
    """Out-of-fold predictions where every row is scored by a model trained
    only on rows strictly before its month (minus the purge). Rows with no
    usable history get None. `fit_predict(train_idx, valid_idx) -> list`."""
    out: list[float | None] = [None] * len(times)
    for fold in purged_walk_forward(times, purge=purge, min_train=min_train, min_valid=1):
        for i, p in zip(fold.valid, fit_predict(fold.train, fold.valid)):
            out[i] = float(p)
    return out
