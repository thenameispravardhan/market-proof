"""Backtest performance metrics — pure functions.

Every function in this module takes a sequence of numbers (or a
list of trade dicts) and returns a number. No DB, no I/O, no
side-effects. The `summarise` helper bundles them into a single
dict the engine writes to `backtest_runs.results["metrics"]`.

Conventions:

  - PnL numbers are in INR (the paper-fill currency). All ratios
    are dimensionless.
  - `equity_curve` is a list of `{date, equity}` dicts in
    chronological order. `equity[0] == initial_capital` (or close).
  - `daily_returns` is the per-period percentage change in equity.
    For a 30-day backtest with end-of-day equity points, this is
    30 values; for an N-trade backtest it's N values around each
    trade.
  - Sharpe is *annualised* assuming 252 trading days. We use the
    simple per-period Sharpe: `mean(returns) / std(returns) *
    sqrt(periods_per_year)`. The convention is `pct` returns
    (0.01 = 1%), not decimal (0.01 = 100%).

Definitions (the deliverable's "metrics definitions" section is
just a copy of these):

  total_return      = end_equity - start_equity
  total_return_pct  = (end_equity - start_equity) / start_equity * 100
  sharpe_ratio      = mean(daily_returns) / std(daily_returns)
                      * sqrt(252)            # annualised
  max_drawdown      = max peak-to-trough drop in equity over the
                      series (positive number, e.g. 10000 means
                      we lost 10,000 INR peak-to-trough)
  max_drawdown_pct  = max_drawdown / peak * 100
  win_rate          = wins / total_trades
  avg_win           = mean(pnl for pnl in trades if pnl > 0)
  avg_loss          = mean(pnl for pnl in trades if pnl < 0)
                      (reported as a positive number; the engine
                       stores the raw negative value too)
  profit_factor     = sum(wins) / abs(sum(losses))
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Sequence


# Annualisation factor — NSE has ~252 trading days/year. For a
# higher-frequency backtest (intraday), the caller should pre-aggregate
# to a per-day series before calling `sharpe_ratio`.
TRADING_DAYS_PER_YEAR: int = 252


# ---- Equity-curve based metrics ----------------------------------------


def total_return(equity: Sequence[float]) -> float:
    """end - start. Empty series → 0."""
    if not equity:
        return 0.0
    return float(equity[-1]) - float(equity[0])


def total_return_pct(equity: Sequence[float]) -> float:
    """(end - start) / start * 100. Returns 0 when start is 0/empty."""
    if len(equity) < 2 or float(equity[0]) == 0.0:
        return 0.0
    return (float(equity[-1]) - float(equity[0])) / float(equity[0]) * 100.0


def max_drawdown(equity: Sequence[float]) -> float:
    """Largest peak-to-trough drop in equity.

    Returns a POSITIVE number (the depth of the loss), so a
    return of 10000 means "we lost 10,000 INR from peak to
    trough at some point". Empty/short series → 0.
    """
    if len(equity) < 2:
        return 0.0
    peak = float(equity[0])
    max_dd = 0.0
    for v in equity:
        fv = float(v)
        if fv > peak:
            peak = fv
        dd = peak - fv
        if dd > max_dd:
            max_dd = dd
    return float(max_dd)


def max_drawdown_pct(equity: Sequence[float]) -> float:
    """`max_drawdown / peak * 100` at the trough's peak. Returns 0
    when the peak is 0."""
    if len(equity) < 2:
        return 0.0
    peak = float(equity[0])
    max_dd = 0.0
    max_dd_pct = 0.0
    for v in equity:
        fv = float(v)
        if fv > peak:
            peak = fv
        if peak > 0:
            dd = peak - fv
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd / peak * 100.0
    return float(max_dd_pct)


def sharpe_ratio(
    daily_returns: Sequence[float],
    *,
    periods_per_year: int = TRADING_DAYS_PER_YEAR,
    risk_free_rate: float = 0.0,
) -> float:
    """Annualised Sharpe ratio of a per-period return series.

    `daily_returns` are PERCENTAGE returns (0.01 = 1% gain). We
    subtract the per-period risk-free rate and annualise by
    `sqrt(periods_per_year)`.

    Returns 0.0 when:
      - fewer than 2 points (no variance)
      - standard deviation is 0 (constant return)

    NB: this is the simple Sharpe. For the "excess" version with
    a non-zero risk-free rate, the caller should subtract the
    per-period rf from each return first.
    """
    if len(daily_returns) < 2:
        return 0.0
    rets = [float(r) for r in daily_returns]
    mean = sum(rets) / len(rets)
    # Subtract the (per-period) risk-free rate.
    excess = mean - float(risk_free_rate)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    std = math.sqrt(var)
    if std <= 0:
        return 0.0
    return float(excess / std * math.sqrt(periods_per_year))


def daily_returns(equity: Sequence[float]) -> list[float]:
    """Convert an equity curve to per-period percentage returns.

    Returns [] for series shorter than 2 points. Returns are
    fractions (0.01 = 1% gain), ready for `sharpe_ratio` after
    multiplying by 100 — but we keep them as fractions here
    and let the caller decide.

    NOTE: the test suite and the engine multiply by 100 to convert
    to percentage points, matching the convention described in
    `summarise` below.
    """
    if len(equity) < 2:
        return []
    out: list[float] = []
    prev = float(equity[0])
    if prev == 0.0:
        return []
    for v in equity[1:]:
        cur = float(v)
        out.append((cur - prev) / prev)
        prev = cur
    return out


# ---- Trade-based metrics -----------------------------------------------


def _trade_pnls(trades: Iterable[Any]) -> list[float]:
    """Extract `pnl` from a list of trade-like objects/dicts.

    Skips trades with a None pnl (still-open positions, expired
    orders, etc.) and rounds to a float. The engine stores the
    raw `pnl` from the paper backend (signed INR), and tests
    pass plain dicts.
    """
    out: list[float] = []
    for t in trades:
        if isinstance(t, dict):
            pnl = t.get("pnl")
        else:
            pnl = getattr(t, "pnl", None)
        if pnl is None:
            continue
        try:
            out.append(float(pnl))
        except (TypeError, ValueError):
            continue
    return out


def win_rate(trades: Iterable[Any]) -> float:
    """Fraction of trades with pnl > 0. Returns 0.0 for an empty list."""
    pnls = _trade_pnls(trades)
    if not pnls:
        return 0.0
    wins = sum(1 for p in pnls if p > 0)
    return float(wins) / float(len(pnls))


def avg_win(trades: Iterable[Any]) -> float:
    """Mean PnL of winning trades. 0.0 when no winners (NOT NaN)."""
    wins = [p for p in _trade_pnls(trades) if p > 0]
    if not wins:
        return 0.0
    return float(sum(wins) / len(wins))


def avg_loss(trades: Iterable[Any]) -> float:
    """Mean PnL of losing trades, returned as a NEGATIVE number.

    e.g. avg_loss=-500 means the average losing trade lost 500 INR.
    0.0 when no losers.
    """
    losses = [p for p in _trade_pnls(trades) if p < 0]
    if not losses:
        return 0.0
    return float(sum(losses) / len(losses))


def profit_factor(trades: Iterable[Any]) -> float:
    """sum(wins) / |sum(losses)|. 0.0 when no losses (undefined).

    A profit factor > 1 means wins exceed losses. 2.0 means for
    every 1 INR lost, 2 INR was won.
    """
    pnls = _trade_pnls(trades)
    if not pnls:
        return 0.0
    gross_wins = sum(p for p in pnls if p > 0)
    gross_losses = abs(sum(p for p in pnls if p < 0))
    if gross_losses == 0.0:
        # No losses → infinitely good; we cap at a large finite
        # value so the JSON stays sane. Caller can branch on
        # `total_trades > 0 and all wins`.
        if gross_wins > 0.0:
            return float(gross_wins) * 1000.0
        return 0.0
    return float(gross_wins) / float(gross_losses)


# ---- The big one -------------------------------------------------------


def summarise(
    *,
    initial_capital: float,
    equity_curve: Sequence[float],
    trades: Iterable[Any],
    signals_generated: int,
    blocked_trades: int,
) -> dict[str, Any]:
    """Bundle every metric into one JSON-serialisable dict.

    Shape (the API returns this directly from
    /api/backtest/runs/{id}):

        {
            "initial_capital":   100000.0,
            "final_equity":      102345.6,
            "total_return":      2345.6,
            "total_return_pct":  2.35,
            "sharpe_ratio":      1.42,
            "max_drawdown":      1500.0,
            "max_drawdown_pct":  1.5,
            "win_rate":          0.6,
            "avg_win":           800.0,
            "avg_loss":          -400.0,        # negative
            "profit_factor":     1.8,
            "total_trades":      10,
            "winning_trades":    6,
            "losing_trades":     4,
            "signals_generated": 12,
            "blocked_trades":    2,
        }

    All numbers are rounded to 4 decimal places so the JSON
    doesn't carry 15-digit float artefacts. `None` is never
    returned; an empty equity curve produces a 0.0 in every
    numeric field.
    """
    eq = [float(x) for x in equity_curve]
    pnls = _trade_pnls(trades)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    rets = daily_returns(eq)
    # Convert fraction returns to percentage points for the
    # sharpe input — matches the `0.01 = 1%` convention the
    # docstring describes.
    rets_pct = [r * 100.0 for r in rets]
    return {
        "initial_capital": round(float(initial_capital), 4),
        "final_equity": round(float(eq[-1]) if eq else float(initial_capital), 4),
        "total_return": round(total_return(eq), 4),
        "total_return_pct": round(total_return_pct(eq), 4),
        "sharpe_ratio": round(sharpe_ratio(rets_pct), 4),
        "max_drawdown": round(max_drawdown(eq), 4),
        "max_drawdown_pct": round(max_drawdown_pct(eq), 4),
        "win_rate": round(win_rate(trades), 4),
        "avg_win": round(avg_win(trades), 4),
        "avg_loss": round(avg_loss(trades), 4),
        "profit_factor": round(profit_factor(trades), 4),
        "total_trades": len(pnls),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "signals_generated": int(signals_generated),
        "blocked_trades": int(blocked_trades),
    }
