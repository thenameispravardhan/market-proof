"""The runner's intraday candles from the live feed: built from ticks while the
feed covers the time, a history call otherwise (app/algo/live_candles.py)."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app.algo import data, live_candles as lc

IST = timezone(timedelta(hours=5, minutes=30))
SYM = "NSE:ACME-EQ"


def ts(h: int, m: int, s: int = 0) -> int:
    return int(datetime(2026, 10, 26, h, m, s, tzinfo=IST).timestamp())


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(lc, "_sym", {})
    monkeypatch.setattr(data, "_seeds", {})
    lc.want([SYM])
    yield
    lc.want(())


def feed(t0: int, t1: int, step: int = 10, cum0: float = 1000.0) -> None:
    """A tick every `step` s in [t0, t1): price = 100 + minutes since 09:15, 10 shares a tick."""
    for k, t in enumerate(range(t0, t1, step)):
        lc.on_tick(SYM, {"ltp": 100 + (t - ts(9, 15)) // 60 + (t % 60) / 100, "exch_feed_time": t,
                         "vol_traded_today": cum0 + 10 * (k + 1)}, t + 0.3)


def test_five_minute_candles_from_ticks():
    feed(ts(9, 18), ts(9, 30, 1))
    got = lc.bars(SYM, 300, ts(9, 20), ts(9, 30, 3))
    assert [r[0] for r in got] == [ts(9, 20), ts(9, 25)]
    t, o, h, l, c, v = got[0]
    assert (o, h, l, c, v) == (105.0, 109.5, 105.0, 109.5, 300.0)   # 30 ticks x 10 shares
    assert lc.bars(SYM, 300, ts(9, 20), ts(9, 29)) == [got[0]]     # 09:25 isn't closed yet


def test_a_gap_in_the_feed_means_no_candles():
    feed(ts(9, 18), ts(9, 22))
    feed(ts(9, 24), ts(9, 30, 1), cum0=2000)                     # two silent minutes
    assert lc.bars(SYM, 300, ts(9, 25), ts(9, 30, 3)) is None
    assert lc.bars(SYM, 300, ts(9, 20), ts(9, 30, 3)) is None
    assert lc.bars("NSE:OTHER-EQ", 300, ts(9, 20), ts(9, 30, 3)) is None


def test_recent_bars_asks_fyers_once_then_reads_the_feed(monkeypatch):
    calls = []

    class B:
        async def get_history_range(self, fy, resolution, from_ts, to_ts):
            calls.append(to_ts)
            return [[t, 99.0, 99.5, 98.5, 99.2, 500] for t in range(ts(9, 15) - 300 * 40, to_ts, 300)
                    if t + 300 <= to_ts]

    monkeypatch.setattr(data, "_backend", lambda: B())
    feed(ts(9, 18), ts(9, 20, 5))
    first = asyncio.run(data.recent_bars(SYM, 5, bars=30, now=ts(9, 20, 3)))
    assert len(calls) == 1 and first["t"][-1] == ts(9, 15)
    feed(ts(9, 20, 5), ts(9, 30, 6), cum0=1130)                # through a tick stamped after 09:30
    later = asyncio.run(data.recent_bars(SYM, 5, bars=30, now=ts(9, 30, 3)))
    assert len(calls) == 1                                       # no second history call
    assert later["t"][-3:] == [ts(9, 15), ts(9, 20), ts(9, 25)] and len(later["t"]) == 30
    assert later["c"][-1] == 114.55 and later["v"][-2] == 310       # 31 ticks: 09:20:00 + 09:20:05..09:24:55

    lc.want(())                                                  # the switch off: history as before
    asyncio.run(data.recent_bars(SYM, 5, bars=30, now=ts(9, 30, 3)))
    assert len(calls) == 2
