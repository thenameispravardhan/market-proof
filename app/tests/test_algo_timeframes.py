"""Strategy timeframes past the intraday ones: 75 / 125 / 180 / 240 minutes,
daily and weekly — in the spec checks, the backtest, the candle store, the
live candle fetch and the live runner's once-a-session schedule."""
from __future__ import annotations

import asyncio
from array import array

import duckdb
import pytest

from app.algo import data, engine
from app.algo import indicators as ind
from app.algo import runner as rn
from app.db import session as dbs
from app.db.models import AlgoStrategy, AlgoTrade

DAY = 86400
D0 = 20454 * DAY - 19800            # 2026-01-01 00:00 IST (a Thursday), epoch seconds
OPEN, CLOSE = 33300, 55800          # 09:15 / 15:30 IST


def _ema_spec(tf, **kw):
    cross = lambda how: {"logic": "AND", "conditions": [  # noqa: E731
        {"left": {"ind": "EMA", "params": {"period": 3}}, "op": how, "right": {"ind": "EMA", "params": {"period": 6}}}]}
    return engine.normalize({"symbols": ["NSE:TEST-EQ"], "timeframe": tf, "direction": "long",
                             "entry_long": cross("crosses_above"), "exit_long": cross("crosses_below"),
                             "stop_loss": None, "target": None, "costs": {"slippage_pct": 0, "charges": False}, **kw})


def _daily(closes, start_day=0):
    """Daily candles as a strategy's own timeframe: 09:15 stamps, tc at 15:30, weekdays only."""
    days = [d for d in range(start_day, start_day + 3 * len(closes)) if (20454 + d + 3) % 7 < 5][:len(closes)]
    opens = [closes[0]] + closes[:-1]
    return {"t": [D0 + d * DAY + OPEN for d in days], "tc": [D0 + d * DAY + CLOSE for d in days],
            "o": opens, "h": [max(o, c) + 0.5 for o, c in zip(opens, closes)],
            "l": [min(o, c) - 0.5 for o, c in zip(opens, closes)], "c": list(closes), "v": [1000] * len(closes),
            "tf_s": DAY, "tf_min": 1440}


# ---- spec ------------------------------------------------------------------

def test_daily_and_weekly_carry_and_have_no_entry_window():
    for tf in (1440, 10080):
        s = _ema_spec(tf, session={"start": "09:30", "end": "14:00", "square_off": "15:15", "days": [1, 2]})
        assert s["session"] == {"start": None, "end": None, "square_off": None, "days": [1, 2]}
        assert engine.session_window(s)[2] == engine.CARRY
    for tf in (75, 125, 180, 240):
        assert _ema_spec(tf)["timeframe"] == tf
    with pytest.raises(ValueError, match="intraday"):
        _ema_spec(1440, bars={"type": "volume", "per_day": 20})
    with pytest.raises(ValueError, match="order-flow"):
        engine.normalize({**_ema_spec(10080), "entry_long": {"logic": "AND", "conditions": [
            {"left": {"ind": "DELTA"}, "op": ">", "right": {"value": 0}}]}})


def test_condition_timeframes_follow_the_strategy_s():
    def on(base, tf):
        return engine.normalize({**_ema_spec(base), "entry_long": {"logic": "AND", "conditions": [
            {"left": {"ind": "PRICE"}, "op": ">", "right": {"ind": "EMA", "params": {"period": 5}, "tf": tf}}]}})
    on(15, 75)
    on(15, 1440)
    on(15, 10080)
    on(1440, 10080)
    for base, tf in ((1440, 15), (1440, 60), (10080, 1440), (75, 120), (60, 30)):
        with pytest.raises(ValueError, match="condition"):
            on(base, tf)
    assert engine.per_session(1440) == 1 and engine.per_session(10080) == 0.2 and engine.per_session(75) == 5


# ---- backtest --------------------------------------------------------------

def test_a_daily_signal_fills_at_the_next_session_open_and_carries():
    closes = [100, 99, 98, 97, 96, 95, 94, 96, 99, 103, 107, 110, 111, 109, 105, 100, 96, 93]
    d = _daily(closes)
    tr = engine.run(_ema_spec(1440), {"NSE:TEST-EQ": d})["trades"]
    assert tr, "the EMA 3/6 cross must trade"
    t = tr[0]
    sig = next(i for i in range(len(d["t"])) if d["t"][i] < t["entry_t"] and d["t"][i + 1] == t["entry_t"])
    assert (t["entry_t"] + 19800) % DAY == OPEN and t["entry"] == d["o"][sig + 1]     # next session's 09:15 open
    assert t["exit_t"] - t["entry_t"] >= DAY and t["reason"] == "SIGNAL"              # carried overnight, no EOD
    assert all(x["reason"] != "EOD" for x in tr)


def test_the_last_candle_of_a_session_closes_at_15_30():
    d = {"t": [D0 + OPEN, D0 + OPEN + 240 * 60], "tf_s": 240 * 60}
    assert ind.close_time(d, 0) == D0 + OPEN + 240 * 60
    assert ind.close_time(d, 1) == D0 + CLOSE                  # 13:15 + 4h would be 17:15
    w = {"t": [D0 + OPEN], "tc": [D0 + 4 * DAY + CLOSE], "tf_s": 7 * DAY}
    assert engine._close_times(w) == [D0 + 4 * DAY + CLOSE]   # a week closes on its last session


# ---- candle store ----------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """15 weekdays of 1-minute candles from Thu 2026-01-01 (Mon 05-Fri 09, Mon 12-Fri 16, + Thu/Fri 01-02)."""
    from app.services import warehouse_prices

    monkeypatch.setattr(warehouse_prices, "CANDLES", tmp_path)
    days = [d for d in range(0, 21) if (20454 + d + 3) % 7 < 5][:12]
    rows = ", ".join(f"({d})" for d in days)
    duckdb.connect().execute(f"""COPY (
        SELECT {D0} + d * 86400 + {OPEN} + m * 60 AS ts, to_timestamp({D0} + d * 86400 + {OPEN} + m * 60)::TIMESTAMP AS datetime,
               100 + d + m / 1000.0 AS open, 101 + d + m / 1000.0 AS high, 99 + d + m / 1000.0 AS low,
               100.5 + d + m / 1000.0 AS close, 10::BIGINT AS volume
        FROM (VALUES {rows}) v(d), range(375) r(m)) TO '{(tmp_path / "TEST.parquet").as_posix()}' (FORMAT parquet)""")
    return days


def test_weekly_candles_from_the_store(store):
    w = data.load("TEST", 10080, D0, D0 + 30 * DAY)
    first = [D0 + d * DAY + OPEN for d in store]
    assert list(w["t"]) == [first[0], first[2], first[7]]            # Thu 01 (its week), Mon 05, Mon 12
    assert list(w["tc"]) == [D0 + 1 * DAY + CLOSE, D0 + 8 * DAY + CLOSE, D0 + 15 * DAY + CLOSE]
    assert list(w["v"]) == [2 * 3750, 5 * 3750, 5 * 3750]
    assert w["o"][1] == 104 and w["c"][1] == 108.5 + 0.374 and w["h"][1] == 101 + 8 + 0.374
    dly = data.as_base(data.load("TEST", 1440, D0, D0 + 30 * DAY), 1440)
    assert dly["t"][0] == D0 + OPEN and dly["tc"][0] == D0 + CLOSE and isinstance(dly["t"], array)
    for tf in (75, 125, 240):
        b = data.load("TEST", tf, D0, D0 + DAY)
        assert b["t"][0] == D0 + OPEN and b["t"][1] == D0 + OPEN + tf * 60


# ---- live candles ----------------------------------------------------------

class _Fyers:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    async def get_history_range(self, fy, *, resolution, from_ts, to_ts):
        self.calls.append(resolution)
        return [r for r in self.rows[resolution] if from_ts <= r[0] <= to_ts]


def test_live_75m_candles_are_built_from_15m_and_only_finished_ones_kept(monkeypatch):
    m15 = [[D0 + OPEN + k * 900, 100 + k, 101 + k, 99 + k, 100.5 + k, 10] for k in range(25)]
    b = _Fyers({"15": m15})
    monkeypatch.setattr(data, "_backend", lambda: b)
    now = D0 + OPEN + 11 * 900 + 30                  # 11 x 15m finished: two 75m candles done, the third forming
    out = asyncio.run(data.recent_bars("NSE:TEST-EQ", 75, bars=10, now=now))
    assert b.calls == ["15"]
    assert out["t"] == [D0 + OPEN, D0 + OPEN + 4500] and out["tf_s"] == 4500
    assert out["o"] == [100, 105] and out["c"] == [104.5, 109.5] and out["v"] == [50, 50]


def test_live_weekly_candles_come_from_daily_and_say_the_last_session(monkeypatch):
    days = [d for d in range(0, 21) if (20454 + d + 3) % 7 < 5]
    dly = [[D0 + d * DAY, 100 + d, 101 + d, 99 + d, 100.5 + d, 10] for d in days]
    b = _Fyers({"D": dly})
    monkeypatch.setattr(data, "_backend", lambda: b)
    mon = D0 + 11 * DAY                               # Mon 12 Jan, 09:20: last week is finished, this one isn't
    out = asyncio.run(data.recent_bars("NSE:TEST-EQ", 10080, bars=5, now=mon + OPEN + 300))
    assert out["t"] == [D0 + OPEN, D0 + 4 * DAY + OPEN]
    assert out["tc"] == [D0 + DAY + CLOSE, D0 + 8 * DAY + CLOSE]
    assert out["last_session"] == D0 + 8 * DAY               # Friday 09: today is the week's first session


# ---- live runner -----------------------------------------------------------

def _paper(tf, **kw):
    spec = _ema_spec(tf, **kw)
    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name=f"tf{tf}", spec=spec, enabled=True, mode="paper")
        db.add(s)
        db.commit()
        return s.id


def _live(monkeypatch, d, *, opened=True):
    seen = {"bundles": 0, "probes": 0}

    async def bundle(spec, sym, now):
        seen["bundles"] += 1
        return {**d, "meta": {"symbol": sym, "name": "TEST", "exch": "NSE"}, "htf": {}}

    async def probe(fy, tf, bars=3, now=None):
        seen["probes"] += 1
        return {"t": [int(now) - 120] if opened else [int(now) - 5 * DAY], "o": [], "h": [], "l": [], "c": [], "v": []}

    async def ltp(symbols):
        return {s.upper(): 120.0 for s in symbols}

    monkeypatch.setattr(rn.data, "live_bundle", bundle)
    monkeypatch.setattr(rn.data, "recent_bars", probe)
    monkeypatch.setattr(rn, "_ltp", ltp)
    monkeypatch.setattr(rn, "_notify_entry", lambda p: None)
    monkeypatch.setattr(rn, "_notify_exit", lambda p: None)
    return seen


def _rising(n=12):
    """Daily candles ending in an EMA 3/6 cross up on the last one."""
    return _daily([100 - k for k in range(n - 2)] + [100 - n + 6, 100])


def _next_session(d):
    """IST day number of the first weekday after the last candle's."""
    k = (d["t"][-1] - OPEN - D0) // DAY + 20454 + 1
    while (k + 3) % 7 >= 5:
        k += 1
    return k


def test_a_daily_strategy_enters_once_at_the_open(monkeypatch, isolated_db):
    d = _rising()
    assert engine.signals(d, _ema_spec(1440), {})["entry_long"][-1]
    seen = _live(monkeypatch, d)
    _paper(1440)
    r = rn.AlgoRunner()
    t0 = _next_session(d) * DAY - 19800 + OPEN                  # the session after the signal candle
    asyncio.run(r.tick(t0 + 30))                                  # too early: the open isn't confirmed yet
    assert seen["bundles"] == 0
    asyncio.run(r.tick(t0 + rn.OPEN_WAIT_S + 5))
    asyncio.run(r.tick(t0 + rn.OPEN_WAIT_S + 60))                 # same session: not again
    with dbs.SessionLocal() as db:
        trades = db.query(AlgoTrade).all()
    assert len(trades) == 1 and trades[0].side == "BUY" and seen["bundles"] == 1


def test_a_daily_strategy_skips_a_holiday_and_a_missed_open(monkeypatch, isolated_db):
    d = _rising()
    t0 = _next_session(d) * DAY - 19800 + OPEN
    seen = _live(monkeypatch, d, opened=False)
    _paper(1440)
    asyncio.run(rn.AlgoRunner().tick(t0 + 11 * 60))              # no candle today 11 min in: a holiday
    seen2 = _live(monkeypatch, d)
    asyncio.run(rn.AlgoRunner().tick(t0 + rn.MULTI_DAY_WINDOW_S + 60))   # started too late
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).count() == 0
    assert seen["bundles"] == 0 and seen2["bundles"] == 0


def test_a_weekly_strategy_acts_only_on_the_week_s_first_session(monkeypatch, isolated_db):
    d = _rising()
    d = {**d, "tf_s": 7 * DAY, "tf_min": 10080}
    monday = 20454 + 11                                           # Mon 12 Jan 2026 (IST day number)
    seen = _live(monkeypatch, {**d, "last_session": (monday - 1) * DAY - 19800})       # Friday before: a new week
    _paper(10080)
    asyncio.run(rn.AlgoRunner().tick((monday + 1) * DAY - 19800 + OPEN + 120))         # Tue, its first session
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).count() == 1
    seen = _live(monkeypatch, {**d, "last_session": (monday + 1) * DAY - 19800})       # Tue already traded
    with dbs.SessionLocal() as db:
        db.query(AlgoTrade).delete()
        db.commit()
    asyncio.run(rn.AlgoRunner().tick((monday + 2) * DAY - 19800 + OPEN + 120))         # Wed: not a new week
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).count() == 0
    assert seen["bundles"] == 1
