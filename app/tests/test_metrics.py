"""Tests for the backtest metrics module.

Coverage:
  - total_return / total_return_pct on a hand-calculated series
  - sharpe_ratio (constant, increasing, noisy, single point)
  - max_drawdown / max_drawdown_pct
  - win_rate / avg_win / avg_loss / profit_factor on a hand-
    calculated example
  - summarise bundles everything correctly
  - edge cases: empty list, single point, negative-only trades
"""
from __future__ import annotations

import math

import pytest

from app.backtest.metrics import (
    TRADING_DAYS_PER_YEAR,
    avg_loss,
    avg_win,
    daily_returns,
    max_drawdown,
    max_drawdown_pct,
    profit_factor,
    sharpe_ratio,
    summarise,
    total_return,
    total_return_pct,
    win_rate,
)


# ---- total_return / total_return_pct -----------------------------------


def test_total_return_simple():
    assert total_return([100.0, 110.0, 105.0]) == 5.0


def test_total_return_empty():
    assert total_return([]) == 0.0


def test_total_return_single():
    assert total_return([100.0]) == 0.0


def test_total_return_pct_simple():
    # 100 -> 110 = 10%
    assert total_return_pct([100.0, 110.0]) == pytest.approx(10.0)


def test_total_return_pct_empty():
    assert total_return_pct([]) == 0.0


def test_total_return_pct_zero_start():
    # 0 -> 100 → 0 (avoid ZeroDivisionError)
    assert total_return_pct([0.0, 100.0]) == 0.0


def test_total_return_pct_loss():
    # 100 -> 80 = -20%
    assert total_return_pct([100.0, 80.0]) == pytest.approx(-20.0)


# ---- sharpe_ratio ------------------------------------------------------


def test_sharpe_ratio_constant_returns():
    # Constant returns → std = 0 → 0.0
    assert sharpe_ratio([0.01, 0.01, 0.01, 0.01]) == 0.0


def test_sharpe_ratio_too_few_points():
    assert sharpe_ratio([0.01]) == 0.0
    assert sharpe_ratio([]) == 0.0


def test_sharpe_ratio_hand_calculated():
    """Hand-calculated: returns = [1%, 2%, -1%, 3%]
    mean = 0.0125, std (sample) = sqrt(((1-1.25)^2 + (2-1.25)^2
    + (-1-1.25)^2 + (3-1.25)^2) / 3) = sqrt(0.0625 + 0.5625
    + 5.0625 + 3.0625) / 3 = sqrt(8.75/3) = sqrt(2.9167) ≈ 1.7078
    sharpe = 0.0125 / 1.7078 * sqrt(252) ≈ 0.1153
    """
    rets_pct = [1.0, 2.0, -1.0, 3.0]
    expected = (1.25 / 1.7078) * math.sqrt(252)
    # tolerance: the test's hard-coded mean (1.25) is the same as
    # the implementation's. We compute the std to 4 sig figs.
    expected = (1.25 / math.sqrt(8.75 / 3)) * math.sqrt(252)
    assert sharpe_ratio(rets_pct) == pytest.approx(expected, rel=1e-3)


def test_sharpe_ratio_positive_trend_higher_than_negative():
    """A consistent positive-return series should have a higher
    Sharpe than a consistent negative-return series of the same
    volatility."""
    positive = [0.5, 0.6, 0.4, 0.7, 0.5, 0.6]
    negative = [-0.5, -0.6, -0.4, -0.7, -0.5, -0.6]
    assert sharpe_ratio(positive) > sharpe_ratio(negative)


# ---- max_drawdown ------------------------------------------------------


def test_max_drawdown_simple():
    # Equity: 100 → 120 → 80 → 110 → 90
    # Peak = 120, then drop to 80 = 40 drawdown. Later peak 120 again,
    # drop to 90 = 30. So max drawdown = 40.
    eq = [100.0, 120.0, 80.0, 110.0, 90.0]
    assert max_drawdown(eq) == pytest.approx(40.0)


def test_max_drawdown_no_drawdown():
    assert max_drawdown([100.0, 110.0, 120.0, 130.0]) == 0.0


def test_max_drawdown_empty():
    assert max_drawdown([]) == 0.0


def test_max_drawdown_pct_simple():
    # Peak 120, trough 80 → 40/120 = 33.33%
    eq = [100.0, 120.0, 80.0, 110.0]
    assert max_drawdown_pct(eq) == pytest.approx(33.333, rel=1e-3)


# ---- trade-based metrics ----------------------------------------------


def test_win_rate_simple():
    trades = [{"pnl": 100}, {"pnl": -50}, {"pnl": 200}, {"pnl": -10}]
    assert win_rate(trades) == pytest.approx(0.5)


def test_win_rate_empty():
    assert win_rate([]) == 0.0


def test_win_rate_no_pnl():
    trades = [{"pnl": None}, {"pnl": 100}]
    # None pnl is skipped; 1 of 1 = 1.0
    assert win_rate(trades) == 1.0


def test_avg_win():
    trades = [{"pnl": 100}, {"pnl": 200}, {"pnl": -50}]
    assert avg_win(trades) == pytest.approx(150.0)


def test_avg_win_no_winners():
    assert avg_win([{"pnl": -50}]) == 0.0


def test_avg_loss_negative():
    trades = [{"pnl": 100}, {"pnl": -200}, {"pnl": -400}]
    assert avg_loss(trades) == pytest.approx(-300.0)


def test_avg_loss_no_losers():
    assert avg_loss([{"pnl": 100}]) == 0.0


def test_profit_factor_simple():
    # wins = 100+200 = 300, losses = 50+150 = 200 → 1.5
    trades = [{"pnl": 100}, {"pnl": -50}, {"pnl": 200}, {"pnl": -150}]
    assert profit_factor(trades) == pytest.approx(1.5)


def test_profit_factor_no_losses_returns_large():
    # No losses → returns wins * 1000 (capped finite value).
    val = profit_factor([{"pnl": 100}, {"pnl": 200}])
    assert val > 100.0


def test_profit_factor_empty():
    assert profit_factor([]) == 0.0


# ---- daily_returns ----------------------------------------------------


def test_daily_returns_simple():
    rets = daily_returns([100.0, 110.0, 99.0])
    assert rets == pytest.approx([0.1, -0.1])


def test_daily_returns_empty():
    assert daily_returns([]) == []


def test_daily_returns_single():
    assert daily_returns([100.0]) == []


# ---- summarise --------------------------------------------------------


def test_summarise_basic():
    equity = [100.0, 110.0, 120.0, 115.0]
    trades = [{"pnl": 200}, {"pnl": -100}, {"pnl": 150}]
    summary = summarise(
        initial_capital=100.0,
        equity_curve=equity,
        trades=trades,
        signals_generated=5,
        blocked_trades=2,
    )
    assert summary["initial_capital"] == 100.0
    assert summary["final_equity"] == 115.0
    assert summary["total_return"] == 15.0
    assert summary["total_return_pct"] == pytest.approx(15.0)
    assert summary["max_drawdown"] == pytest.approx(5.0)
    assert summary["win_rate"] == pytest.approx(round(2 / 3, 4))
    assert summary["avg_win"] == pytest.approx(175.0)
    assert summary["avg_loss"] == pytest.approx(-100.0)
    assert summary["total_trades"] == 3
    assert summary["winning_trades"] == 2
    assert summary["losing_trades"] == 1
    assert summary["signals_generated"] == 5
    assert summary["blocked_trades"] == 2


def test_summarise_empty():
    summary = summarise(
        initial_capital=50_000.0,
        equity_curve=[50_000.0],
        trades=[],
        signals_generated=0,
        blocked_trades=0,
    )
    assert summary["initial_capital"] == 50_000.0
    assert summary["final_equity"] == 50_000.0
    assert summary["total_return"] == 0.0
    assert summary["total_trades"] == 0
    assert summary["win_rate"] == 0.0
    assert summary["avg_win"] == 0.0
    assert summary["avg_loss"] == 0.0


def test_summarise_trading_days_constant():
    """The TRADING_DAYS_PER_YEAR constant is 252 (NSE)."""
    assert TRADING_DAYS_PER_YEAR == 252
