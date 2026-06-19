"""IST market-session clock (RISK.md §1, §5).

Pure helpers over `datetime` that answer three questions the risk
layer needs:

  - `is_market_open(now)`    — are we inside the trading session at all?
  - `is_entry_window(now)`   — may we OPEN a new position now? (excludes
                               the first 15 min after open and the last
                               30 min before close — the noisy edges)
  - `square_off_due(now)`    — is it time to force-flatten intraday
                               positions? (≥ SQUARE_OFF_TIME_IST)

All times come from `Settings` ("HH:MM" IST strings) and are read live,
so an operator can shift the windows without a restart. `now` is an
optional timezone-aware (or naive-UTC) datetime, injectable for tests;
it defaults to the real wall clock.

NOTE (seam): there is no exchange-holiday calendar wired in — these
functions only exclude weekends. A holiday feed can be added later by
extending `_is_trading_day`. Until then, treat a holiday like any open
weekday; the EOD square-off and the broker's own rejection of orders on
a closed exchange are the backstops.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from typing import Optional

from app.config import get_settings

# India Standard Time is a fixed UTC+5:30 offset (no DST).
IST = timezone(timedelta(hours=5, minutes=30))


def _parse_hhmm(value: str) -> time:
    """Parse a validated "HH:MM" string into a `time`. Settings already
    validates the format, so this is a thin, defensive split."""
    hh, mm = value.split(":")
    return time(hour=int(hh), minute=int(mm))


def to_ist(now: Optional[datetime] = None) -> datetime:
    """Return `now` as an IST-aware datetime.

    A naive datetime is assumed to be UTC (the project-wide convention),
    matching how `filed_at` and friends are stored.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(IST)


def _is_trading_day(ist_dt: datetime) -> bool:
    # Monday=0 .. Friday=4 are trading days. Holidays are not modelled
    # (see module docstring).
    return ist_dt.weekday() < 5


def is_market_open(now: Optional[datetime] = None) -> bool:
    """True iff `now` (IST) is a weekday within [MARKET_OPEN, MARKET_CLOSE]."""
    s = get_settings()
    ist = to_ist(now)
    if not _is_trading_day(ist):
        return False
    t = ist.time()
    return _parse_hhmm(s.MARKET_OPEN_IST) <= t <= _parse_hhmm(s.MARKET_CLOSE_IST)


def is_entry_window(now: Optional[datetime] = None) -> bool:
    """True iff a NEW position may be opened now.

    The entry window is a weekday inside [ENTRY_WINDOW_START,
    ENTRY_WINDOW_END] — by default 09:30–15:00 IST, i.e. the session
    minus the first 15 min (opening auction noise) and the last 30 min
    (pre-close mean reversion + square-off run-up).

    When `Settings.ENFORCE_MARKET_HOURS` is False the gate is disabled
    and this always returns True — useful for paper testing / backtests
    where wall-clock time shouldn't block entries.
    """
    s = get_settings()
    if not s.ENFORCE_MARKET_HOURS:
        return True
    ist = to_ist(now)
    if not _is_trading_day(ist):
        return False
    t = ist.time()
    return _parse_hhmm(s.ENTRY_WINDOW_START_IST) <= t <= _parse_hhmm(s.ENTRY_WINDOW_END_IST)


def square_off_due(now: Optional[datetime] = None) -> bool:
    """True iff intraday positions should be force-squared-off now.

    Fires on a trading day once IST time ≥ SQUARE_OFF_TIME (default
    15:10) and up to the close. The EOD scheduler polls this and calls
    `TradeManager.close_all(reason="EOD_SQUAREOFF")` when it flips True.

    Unlike `is_entry_window`, this is NOT disabled by
    `ENFORCE_MARKET_HOURS` — if you square off, you want it to happen on
    the real clock regardless of the entry gate.
    """
    s = get_settings()
    ist = to_ist(now)
    if not _is_trading_day(ist):
        return False
    t = ist.time()
    return _parse_hhmm(s.SQUARE_OFF_TIME_IST) <= t <= _parse_hhmm(s.MARKET_CLOSE_IST)


def entry_block_reason(now: Optional[datetime] = None) -> Optional[str]:
    """Human-readable reason a new entry is blocked by the clock, or
    None when the entry window is open. Handy for risk logs / audit."""
    s = get_settings()
    if not s.ENFORCE_MARKET_HOURS:
        return None
    if is_entry_window(now):
        return None
    if not is_market_open(now):
        return "market_closed"
    if square_off_due(now):
        return "post_squareoff"
    # Open, but inside an excluded edge (first 15 / last 30 minutes).
    return "outside_entry_window"
