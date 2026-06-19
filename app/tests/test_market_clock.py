"""Tests for the IST market-session clock."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.risk import market_clock as mc

IST = mc.IST


@pytest.fixture(autouse=True)
def _enforce_market_hours(monkeypatch):
    # conftest disables market-hours enforcement globally so other suites
    # aren't time-of-day dependent; these tests need it ON to exercise the
    # real entry-window gating.
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _utc_for_ist(year, month, day, hh, mm) -> datetime:
    """Build a UTC-aware datetime for a given IST wall-clock time."""
    ist_dt = datetime(year, month, day, hh, mm, tzinfo=IST)
    return ist_dt.astimezone(timezone.utc)


# 2026-06-19 is a Friday (a trading weekday); 2026-06-20 is a Saturday.
FRI = (2026, 6, 19)
SAT = (2026, 6, 20)


@pytest.mark.parametrize(
    "hh,mm,open_,entry,squareoff",
    [
        (10, 0, True, True, False),    # mid-session: everything go
        (9, 20, True, False, False),   # first 15 min after open: no entry
        (15, 5, True, False, False),   # last 30 min before close: no entry
        (15, 15, True, False, True),   # after 15:10: square-off due
        (16, 0, False, False, False),  # after close
        (8, 0, False, False, False),   # before open
    ],
)
def test_session_windows(hh, mm, open_, entry, squareoff):
    now = _utc_for_ist(*FRI, hh, mm)
    assert mc.is_market_open(now) is open_
    assert mc.is_entry_window(now) is entry
    assert mc.square_off_due(now) is squareoff


def test_weekend_is_always_closed():
    now = _utc_for_ist(*SAT, 10, 0)  # Saturday mid-session-time
    assert mc.is_market_open(now) is False
    assert mc.is_entry_window(now) is False
    assert mc.square_off_due(now) is False


def test_entry_block_reason():
    assert mc.entry_block_reason(_utc_for_ist(*FRI, 10, 0)) is None
    assert mc.entry_block_reason(_utc_for_ist(*FRI, 9, 20)) == "outside_entry_window"
    assert mc.entry_block_reason(_utc_for_ist(*FRI, 15, 15)) == "post_squareoff"
    assert mc.entry_block_reason(_utc_for_ist(*FRI, 16, 0)) == "market_closed"


def test_enforce_market_hours_toggle(monkeypatch):
    # With enforcement off, the entry gate is open even at 3am IST on a
    # Saturday — but square-off still runs on the real clock.
    monkeypatch.setenv("ENFORCE_MARKET_HOURS", "0")
    get_settings.cache_clear()
    try:
        off_hours = _utc_for_ist(*SAT, 3, 0)
        assert mc.is_entry_window(off_hours) is True
        assert mc.is_market_open(off_hours) is False
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


def test_to_ist_treats_naive_as_utc():
    naive_utc = datetime(2026, 6, 19, 4, 30)  # 04:30 UTC == 10:00 IST
    ist = mc.to_ist(naive_utc)
    assert (ist.hour, ist.minute) == (10, 0)
