"""Large universes on the Standard plan's data budget: the screening tolerance,
the approximate candles built from quote snapshots, and the runner trading a
screened universe exactly as it trades the same universe candle by candle."""
from __future__ import annotations

import asyncio
import math
import random
from types import SimpleNamespace

import pytest

from app.algo import engine
from app.algo import runner as rn
from app.algo import screen as sc
from app.db import session as dbs
from app.db.models import AlgoStrategy, AlgoTrade

DAY = 86400
D0 = 20454 * DAY - 19800 + 6 * DAY        # Wed 2026-01-07 00:00 IST
OPEN = 33300


def _spec(n, **kw):
    cross = lambda how: {"logic": "AND", "conditions": [  # noqa: E731
        {"left": {"ind": "EMA", "params": {"period": 3}}, "op": how, "right": {"ind": "EMA", "params": {"period": 8}}}]}
    return engine.normalize({"symbols": [f"NSE:S{k}-EQ" for k in range(n)], "timeframe": 5, "direction": "both",
                             "entry_long": cross("crosses_above"), "entry_short": cross("crosses_below"),
                             "exit_long": cross("crosses_below"), "exit_short": cross("crosses_above"),
                             "stop_loss": None, "target": None, "max_trades_per_day": 20,
                             "session": {"start": "09:15", "end": "15:00", "square_off": "15:15"},
                             "portfolio": {"capital": 1e9, "max_positions": 1000},
                             "costs": {"slippage_pct": 0, "charges": False}, **kw})


@pytest.fixture
def standard(monkeypatch):
    from app.config import get_settings

    st = get_settings()
    for k, v in {"FYERS_PLAN": "standard", "FYERS_DATA_PER_SEC": 0, "FYERS_DATA_PER_MIN": 0, "FYERS_DATA_PER_DAY": 0,
                 "FYERS_WS_SYMBOLS": 0, "FYERS_DATA_RESERVE_PCT": 20.0, "ALGO_SCREEN_SLACK_PCT": 0.5}.items():
        monkeypatch.setattr(st, k, v, raising=False)


# ---- the tolerance -----------------------------------------------------------

def test_screening_is_a_superset_and_negation_stays_one():
    rnd = random.Random(3)
    c = [100 + 3 * math.sin(i / 5) + rnd.uniform(-0.3, 0.3) for i in range(300)]
    d = {"t": list(range(300)), "o": c, "h": c, "l": c, "c": c, "v": [1] * 300, "tf_s": 300}
    spec = _spec(1)
    exact = engine.signals(d, spec, {})
    with engine.screening(0.005):
        loose = engine.signals(d, spec, {})
    for k in exact:
        assert all(b for a, b in zip(exact[k], loose[k]) if a), k            # nothing exact is screened out
    assert sum(loose["entry_long"]) >= sum(exact["entry_long"])
    neg = {**spec, "entry_long": {"logic": "AND", "conditions": [
        {"left": {"ind": "PRICE"}, "op": ">", "right": {"value": 101}, "negate": True}]}}
    ex = engine.signals(d, neg, {})["entry_long"]
    with engine.screening(0.005):
        lo = engine.signals(d, neg, {})["entry_long"]
    assert all(b for a, b in zip(ex, lo) if a) and sum(lo) > sum(ex)       # NOT(x > 101) widens too
    # A noisy copy (errors under the tolerance) still screens in every exact signal.
    noisy = {**d, "c": [x * (1 + rnd.uniform(-0.0004, 0.0004)) for x in c]}
    with engine.screening(0.005):
        lo = engine.signals(noisy, spec, {})["entry_long"]
    assert all(b for a, b in zip(exact["entry_long"], lo) if a)


# ---- approximate candles -----------------------------------------------------

def _bundle(closes, start=D0 - DAY + OPEN, tf=300):
    n = len(closes)
    return sc._compact({"t": [start + i * tf for i in range(n)], "o": list(closes), "h": list(closes),
                        "l": list(closes), "c": list(closes), "v": [10] * n, "tf_s": tf,
                        "htf": {15: {"t": [start], "o": [closes[0]], "h": [max(closes)], "l": [min(closes)],
                                     "c": [closes[-1]], "v": [10 * n], "tf_s": 900}}})


def test_approximate_candles_from_snapshots():
    s = sc.Screener()
    d = _bundle([100.0] * 5)                                  # yesterday's candles
    bar = D0 + OPEN
    assert s._append(d, bar, 300, (0, 101.0, 100.5, 102.0, 99.0, 5000))      # today's first: the day's own O/H/L/V
    assert (d["o"][-1], d["h"][-1], d["l"][-1], d["c"][-1], d["v"][-1]) == (100.5, 102.0, 99.0, 101.0, 5000)
    assert s._append(d, bar + 300, 300, (0, 101.5, 100.5, 102.0, 98.5, 5600))  # only the low moved
    assert (d["o"][-1], d["h"][-1], d["l"][-1], d["c"][-1], d["v"][-1]) == (101.0, 101.5, 98.5, 101.5, 600)
    h15 = d["htf"][15]
    assert h15["t"][-1] == bar and h15["h"][-1] == 102.0 and h15["l"][-1] == 98.5 and h15["v"][-1] == 5600
    assert not s._append(d, bar + 900, 300, (0, 103.0, 100.5, 103.0, 98.5, 6000))   # a missed close: unsure
    assert d["c"][-2] == 101.5 and d["v"][-2] == 0 and d["c"][-1] == 103.0
    assert not s._append(d, bar + 1200, 300, None)


# ---- the runner: screened == candle by candle ---------------------------------

def _market(n, bars, seed=7):
    """n symbols of 5-minute candles over yesterday + today (exact), each with its own wave."""
    rnd = random.Random(seed)
    out = {}
    for k in range(n):
        ph, per = rnd.uniform(0, 6), rnd.uniform(4, 9)
        c = [100 + 4 * math.sin(i / per + ph) + rnd.uniform(-0.2, 0.2) for i in range(bars)]
        o = [c[0]] + c[:-1]
        out[f"NSE:S{k}-EQ"] = {"c": c, "o": o, "h": [max(a, b) + 0.1 for a, b in zip(o, c)],
                               "l": [min(a, b) - 0.1 for a, b in zip(o, c)], "v": [1000 + k] * bars}
    return out


def _times(bars):
    y = [D0 - DAY + OPEN + i * 300 for i in range(75)]
    return (y + [D0 + OPEN + i * 300 for i in range(75)])[:bars]


def _install(monkeypatch, mkt, ts):
    calls = {"bundle": 0, "quote": 0}

    def upto(sym, now):
        m = mkt[sym]
        keep = [i for i, t in enumerate(ts) if t + 300 <= now]
        return {"t": [ts[i] for i in keep], **{k: [m[k][i] for i in keep] for k in "ohlcv"}}

    async def bundle(spec, sym, now):
        calls["bundle"] += 1
        return {**upto(sym, now), "tf_s": 300, "tf_min": 5, "htf": {}, "meta": {"symbol": sym, "name": sym, "exch": "NSE"}}

    class Fyers:
        async def get_quote(self, syms):
            calls["quote"] += 1
            now = rn.time.time()
            out = []
            for x in syms:
                b = upto(x, now)
                today = [i for i, t in enumerate(b["t"]) if t >= D0]
                out.append(SimpleNamespace(symbol=x, last_price=b["c"][-1], volume=sum(b["v"][i] for i in today),
                                           extra={"open": b["o"][today[0]], "high": max(b["h"][i] for i in today),
                                                  "low": min(b["l"][i] for i in today)} if today else {}))
            return out

    async def ltp(symbols):
        now = rn.time.time()
        return {x.upper(): upto(x, now)["c"][-1] for x in symbols if x in mkt}

    monkeypatch.setattr(rn.data, "live_bundle", bundle)
    monkeypatch.setattr(sc.data, "live_bundle", bundle)
    monkeypatch.setattr("app.api.market._fyers_backend", lambda: Fyers())
    monkeypatch.setattr(rn, "_ltp", ltp)
    monkeypatch.setattr(rn, "_notify_entry", lambda p: None)
    monkeypatch.setattr(rn, "_notify_exit", lambda p: None)
    return calls


def _session(monkeypatch, n, screened_cap):
    """Run the runner over today's session; return (trades, calls)."""
    mkt = _market(n, 150)
    ts = _times(150)
    calls = _install(monkeypatch, mkt, ts)
    monkeypatch.setattr(sc, "exact_capacity", lambda spec: screened_cap)
    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="big", spec=_spec(n), enabled=True, mode="paper"))
        db.commit()
    r = rn.AlgoRunner()
    clock = {"t": D0 + 8 * 3600 + 35 * 60}

    async def drive():
        while clock["t"] < D0 + 11 * 3600:               # 08:35 → 11:00, the warm-up, then a morning of candles
            rn.time.time = lambda: clock["t"]            # noqa: B023
            await r.tick(clock["t"])
            if r.screen.warming:
                await r.screen.warming
            for task in list(r._bar_tasks.values()):     # a screened strategy's candle pass runs off the 1 s loop
                await task
            clock["t"] += 20 if clock["t"] < D0 + OPEN else 5
    real = rn.time.time
    try:
        asyncio.run(drive())
    finally:
        rn.time.time = real
    with dbs.SessionLocal() as db:
        trades = sorted((t.symbol, t.side, t.entry_at, t.status) for t in db.query(AlgoTrade).all())
        db.query(AlgoTrade).delete()
        db.query(AlgoStrategy).delete()
        db.commit()
    return trades, calls, r.screen.stats


def test_a_screened_universe_trades_exactly_like_the_candle_by_candle_one(monkeypatch, isolated_db, standard):
    exact, c_exact, _ = _session(monkeypatch, 120, screened_cap=10_000)     # fits: every symbol, every candle
    screened, c_scr, stats = _session(monkeypatch, 120, screened_cap=30)
    assert exact and screened == exact
    assert c_scr["bundle"] < c_exact["bundle"] / 3                          # most candles never fetched
    assert c_scr["quote"] > 0 and stats and all(v["candidates"] <= max(30, v["held"]) for v in stats.values())


def test_a_large_daily_strategy_is_decided_from_its_warm_up_with_no_call_at_the_open(monkeypatch, isolated_db, standard):
    """200 symbols on daily candles: the pre-open warm-up holds yesterday's
    finished candle, so the open needs no candle fetch — and trades the same."""
    rnd = random.Random(5)
    days = [D0 - k * DAY for k in range(40, 0, -1) if ((D0 - k * DAY + 19800) // DAY + 3) % 7 < 5]
    mkt = {}
    for k in range(200):
        ph = rnd.uniform(0, 6)
        c = [100 + 5 * math.sin(i / 3 + ph) for i in range(len(days))]
        mkt[f"NSE:S{k}-EQ"] = c

    def run(cap):
        calls = {"bundle": 0}

        async def bundle(spec, sym, now):
            calls["bundle"] += 1
            c = mkt[sym]
            return {"t": [t + OPEN for t in days], "tc": [t + 55800 for t in days], "o": [c[0]] + c[:-1],
                    "h": [x + 1 for x in c], "l": [x - 1 for x in c], "c": list(c), "v": [10] * len(c),
                    "tf_s": DAY, "tf_min": 1440, "htf": {}, "meta": {"symbol": sym, "name": sym, "exch": "NSE"}}

        async def probe(fy, tf, bars=3, now=None):
            return {"t": [int(now) - 60], "o": [], "h": [], "l": [], "c": [], "v": []}

        async def ltp(symbols):
            return {x.upper(): mkt[x][-1] for x in symbols if x in mkt}

        monkeypatch.setattr(rn.data, "live_bundle", bundle)
        monkeypatch.setattr(sc.data, "live_bundle", bundle)
        monkeypatch.setattr(rn.data, "recent_bars", probe)
        monkeypatch.setattr(rn, "_ltp", ltp)
        monkeypatch.setattr(rn, "_notify_entry", lambda p: None)
        monkeypatch.setattr(sc, "exact_capacity", lambda spec: cap)
        spec = {**_spec(200), "timeframe": 1440}
        with dbs.SessionLocal() as db:
            db.add(AlgoStrategy(name="daily", spec=engine.normalize(spec), enabled=True, mode="paper"))
            db.commit()
        r = rn.AlgoRunner()

        async def drive():
            await r.tick(D0 + 8 * 3600 + 40 * 60)                      # 08:40: warm-up
            if r.screen.warming:
                await r.screen.warming
            warm_calls = calls["bundle"]
            for dt in (rn.OPEN_WAIT_S + 5, rn.OPEN_WAIT_S + 30):
                await r.tick(D0 + OPEN + dt)
                for task in list(r._bar_tasks.values()):
                    await task
            return warm_calls
        warm_calls = asyncio.run(drive())
        with dbs.SessionLocal() as db:
            trades = sorted((t.symbol, t.side) for t in db.query(AlgoTrade).all())
            db.query(AlgoTrade).delete()
            db.query(AlgoStrategy).delete()
            db.commit()
        return trades, warm_calls, calls["bundle"]

    exact, w0, total0 = run(10_000)
    screened, w1, total1 = run(30)
    assert exact and screened == exact
    assert w0 == 0 and total0 == 200                 # not screened: every symbol fetched at the open
    assert w1 == 200 and total1 == 200               # screened: all before the open, none at it
