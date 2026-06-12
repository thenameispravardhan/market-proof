"""Backtest engine and metrics.

The backtest package replays historical announcements through the
existing analyzer → rules engine → risk engine → paper execution
pipeline to show what the bot *would* have done. Results are
persisted to `backtest_runs.results` (JSON) so the UI can chart the
equity curve without re-running.

Modules:

  metrics    — pure functions: total_return, sharpe, max_drawdown,
               win_rate, avg_win, avg_loss, profit_factor, summarise.
  engine     — `BacktestEngine`. Coordinates the replay loop and
               records every decision (analysis / signal / trade /
               risk_block) into `backtest_runs.results`.
"""
from __future__ import annotations

from app.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestProgress,
    BacktestResult,
    PriceModel,
    SyntheticReturnPriceModel,
    run_backtest,
)
from app.backtest.metrics import (
    avg_loss,
    avg_win,
    max_drawdown,
    max_drawdown_pct,
    profit_factor,
    sharpe_ratio,
    summarise,
    total_return,
    total_return_pct,
    win_rate,
)

__all__ = [
    # engine
    "BacktestConfig",
    "BacktestEngine",
    "BacktestProgress",
    "BacktestResult",
    "PriceModel",
    "SyntheticReturnPriceModel",
    "run_backtest",
    # metrics
    "avg_loss",
    "avg_win",
    "max_drawdown",
    "max_drawdown_pct",
    "profit_factor",
    "sharpe_ratio",
    "summarise",
    "total_return",
    "total_return_pct",
    "win_rate",
]
