"""Evaluation statistics with honest uncertainty.

Everything returns plain floats/dicts so the API and the CLI report share
one implementation. Bootstraps are seeded so a report is reproducible.

Includes the two overfitting corrections reviewers ask for when a strategy
was picked by an optimiser:

  * Deflated Sharpe Ratio (Bailey & López de Prado 2014): the probability
    the true Sharpe is above zero after accounting for the number of trials,
    the variance of Sharpe across trials, and the skew/kurtosis of returns.
  * Probability of Backtest Overfitting via combinatorially symmetric
    cross-validation (Bailey et al. 2017): how often the in-sample winner
    ranks below the median out of sample.
"""
from __future__ import annotations

import itertools
import math
import random
from statistics import NormalDist
from typing import Iterable, Optional, Sequence

_N = NormalDist()
EULER_GAMMA = 0.5772156649015329


def mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def stdev(xs: Sequence[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def bootstrap_ci(
    xs: Sequence[float],
    stat=mean,
    *,
    n_boot: int = 2000,
    alpha: float = 0.05,
    seed: int = 7,
) -> tuple[Optional[float], Optional[float]]:
    """Percentile bootstrap CI for `stat` over `xs`."""
    xs = list(xs)
    if len(xs) < 2:
        return (None, None)
    rng = random.Random(seed)
    n = len(xs)
    vals = sorted(
        v for v in (stat([xs[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot))
        if v is not None
    )
    if not vals:
        return (None, None)
    lo = vals[int((alpha / 2) * (len(vals) - 1))]
    hi = vals[int((1 - alpha / 2) * (len(vals) - 1))]
    return (lo, hi)


def max_drawdown(pnl: Sequence[float]) -> float:
    """Largest peak-to-trough fall of the cumulative P&L (a positive number)."""
    peak = cum = 0.0
    worst = 0.0
    for x in pnl:
        cum += x
        peak = max(peak, cum)
        worst = max(worst, peak - cum)
    return worst


def sharpe(returns: Sequence[float], periods_per_year: float = 252.0) -> Optional[float]:
    """Annualised Sharpe of per-period returns (zero risk-free rate)."""
    sd = stdev(returns)
    m = mean(returns)
    if sd is None or m is None or sd == 0:
        return None
    return m / sd * math.sqrt(periods_per_year)


def profit_factor(pnl: Sequence[float]) -> Optional[float]:
    gains = sum(x for x in pnl if x > 0)
    losses = -sum(x for x in pnl if x < 0)
    if losses == 0:
        return None
    return gains / losses


def summarize_r(r: Sequence[float], pnl: Sequence[float], *, seed: int = 7) -> dict:
    """The standard per-group block: n, win rate, expectancy in R with a
    bootstrap CI, profit factor and drawdown in rupees."""
    r = list(r)
    lo, hi = bootstrap_ci(r, seed=seed)
    wins = sum(1 for x in r if x > 0)
    return {
        "n": len(r),
        "win_rate": round(wins / len(r), 4) if r else None,
        "expectancy_r": _rnd(mean(r)),
        "expectancy_r_ci95": [_rnd(lo), _rnd(hi)],
        "ci_excludes_zero": bool(lo is not None and (lo > 0 or hi < 0)),
        "net_pnl": round(sum(pnl), 2),
        "profit_factor": _rnd(profit_factor(pnl)),
        "max_drawdown": round(max_drawdown(pnl), 2),
    }


def _rnd(x: Optional[float], nd: int = 4) -> Optional[float]:
    return None if x is None else round(float(x), nd)


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio
# ---------------------------------------------------------------------------


def _skew_kurt(xs: Sequence[float]) -> tuple[float, float]:
    n = len(xs)
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 == 0:
        return 0.0, 3.0
    m3 = sum((x - m) ** 3 for x in xs) / n
    m4 = sum((x - m) ** 4 for x in xs) / n
    return m3 / m2 ** 1.5, m4 / m2 ** 2


def probabilistic_sharpe(returns: Sequence[float], benchmark_sr: float = 0.0) -> Optional[float]:
    """PSR: P(true per-period Sharpe > benchmark_sr) given non-normal returns."""
    n = len(returns)
    sd = stdev(returns)
    if n < 3 or not sd:
        return None
    sr = (sum(returns) / n) / sd
    skew, kurt = _skew_kurt(returns)
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    return _N.cdf((sr - benchmark_sr) * math.sqrt(n - 1) / math.sqrt(denom))


def expected_max_sharpe(n_trials: int, var_sharpe: float) -> float:
    """E[max] of `n_trials` per-period Sharpe estimates under the null of
    zero true Sharpe (the False Strategy Theorem)."""
    if n_trials <= 1 or var_sharpe <= 0:
        return 0.0
    sd = math.sqrt(var_sharpe)
    return sd * ((1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n_trials)
                 + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n_trials * math.e)))


def deflated_sharpe(returns: Sequence[float], *, n_trials: int, trial_sharpes: Optional[Sequence[float]] = None
                    ) -> Optional[dict]:
    """DSR of the selected strategy's per-period `returns`.

    `n_trials` must be the honest count of configurations the optimiser
    tried. `trial_sharpes` (per-period Sharpe of every trial) gives the
    cross-trial variance; without it the selected series' own Sharpe
    standard error is used, which understates the deflation."""
    n = len(returns)
    sd = stdev(returns)
    if n < 3 or not sd:
        return None
    sr = (sum(returns) / n) / sd
    if trial_sharpes and len(trial_sharpes) > 1:
        var_sr = stdev(list(trial_sharpes)) ** 2
    else:
        var_sr = 1.0 / (n - 1)
    sr0 = expected_max_sharpe(int(n_trials), var_sr)
    dsr = probabilistic_sharpe(returns, benchmark_sr=sr0)
    return {"sharpe_per_period": round(sr, 4), "benchmark_sharpe": round(sr0, 4),
            "n_trials": int(n_trials), "dsr": None if dsr is None else round(dsr, 4)}


# ---------------------------------------------------------------------------
# Probability of Backtest Overfitting (CSCV)
# ---------------------------------------------------------------------------


def pbo_cscv(matrix: Sequence[Sequence[float]], n_splits: int = 8) -> Optional[dict]:
    """PBO from a T x N matrix of per-period returns (rows = periods,
    columns = the N configurations the optimiser chose between).

    The rows are cut into `n_splits` contiguous blocks. For every way of
    picking half the blocks as in-sample, the best in-sample column is
    found and its out-of-sample rank recorded. PBO is the share of splits
    where that winner lands in the bottom half out of sample."""
    rows = [list(r) for r in matrix]
    if not rows or len(rows[0]) < 2 or n_splits < 2 or n_splits % 2:
        return None
    n_cols = len(rows[0])
    block = len(rows) // n_splits
    if block < 2:
        return None
    blocks = [rows[i * block:(i + 1) * block] for i in range(n_splits)]

    def perf(sel_rows: list[list[float]], j: int) -> float:
        col = [r[j] for r in sel_rows]
        sd = stdev(col)
        return (sum(col) / len(col)) / sd if sd else 0.0

    logits = []
    for is_idx in itertools.combinations(range(n_splits), n_splits // 2):
        is_rows = [r for i in is_idx for r in blocks[i]]
        oos_rows = [r for i in range(n_splits) if i not in is_idx for r in blocks[i]]
        is_perf = [perf(is_rows, j) for j in range(n_cols)]
        best = max(range(n_cols), key=lambda j: is_perf[j])
        oos_perf = [perf(oos_rows, j) for j in range(n_cols)]
        rank = sum(1 for v in oos_perf if v <= oos_perf[best]) / (n_cols + 1)
        rank = min(max(rank, 1e-6), 1 - 1e-6)
        logits.append(math.log(rank / (1 - rank)))
    pbo = sum(1 for lg in logits if lg <= 0) / len(logits)
    return {"pbo": round(pbo, 4), "splits": len(logits), "n_configs": n_cols}


def ece(probs: Iterable[float], outcomes: Iterable[int], n_bins: int = 10) -> dict:
    """Expected calibration error and the reliability-diagram bins."""
    pairs = [(float(p), int(o)) for p, o in zip(probs, outcomes) if p is not None]
    bins = [[] for _ in range(n_bins)]
    for p, o in pairs:
        k = min(n_bins - 1, max(0, int(p * n_bins)))
        bins[k].append((p, o))
    total = len(pairs)
    err = 0.0
    out = []
    for k, b in enumerate(bins):
        if not b:
            out.append({"lo": k / n_bins, "hi": (k + 1) / n_bins, "n": 0,
                        "mean_confidence": None, "observed_rate": None})
            continue
        conf = sum(p for p, _ in b) / len(b)
        rate = sum(o for _, o in b) / len(b)
        err += len(b) / total * abs(conf - rate)
        out.append({"lo": k / n_bins, "hi": (k + 1) / n_bins, "n": len(b),
                    "mean_confidence": round(conf, 4), "observed_rate": round(rate, 4)})
    return {"ece": round(err, 4) if total else None, "n": total, "bins": out}


def roc_auc(scores: Sequence[float], labels: Sequence[int]) -> Optional[float]:
    """Rank-based AUC (ties averaged). None if one class is missing."""
    pairs = sorted(zip(scores, labels), key=lambda t: t[0])
    n_pos = sum(1 for _, y in pairs if y)
    n_neg = len(pairs) - n_pos
    if not n_pos or not n_neg:
        return None
    rank_sum = 0.0
    i = 0
    while i < len(pairs):
        j = i
        while j + 1 < len(pairs) and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + j) / 2 + 1
        rank_sum += avg * sum(1 for k in range(i, j + 1) if pairs[k][1])
        i = j + 1
    return (rank_sum - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg)
