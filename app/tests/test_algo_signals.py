"""Algo Lab v3, half A: conditions on another symbol's candles, ranking, trial counts, drift."""
from __future__ import annotations

import asyncio

import pytest

from app.algo import data, engine
from app.tests.test_algo import DAY0, _cross_spec, bars

REF = "NSE:NIFTY50-INDEX"


def _ref_spec(**kw):
    """Long the strategy symbol when the INDEX crosses above 105, out when it crosses below 103."""
    return _cross_spec(
        entry_long={"logic": "AND", "conditions": [
            {"left": {"ind": "PRICE", "symbol": REF}, "op": "crosses_above", "right": {"value": 105}}]},
        exit_long={"logic": "AND", "conditions": [
            {"left": {"ind": "PRICE", "symbol": REF}, "op": "crosses_below", "right": {"value": 103}}]}, **kw)


# ---- A1: operands on another symbol ------------------------------------------

def test_operand_on_another_symbol_sees_only_its_closed_candles():
    d = bars([100.0] * 6)
    ref = bars([10.0, 11, 12, 13, 14, 15])
    ref["htf"] = {15: bars([30.0, 31], tf=900)}
    d["xs"] = {REF: ref}
    assert engine.series(d, {"ind": "PRICE", "symbol": REF}, {}) == [10.0, 11, 12, 13, 14, 15]
    # its own higher timeframe: a 15m candle is seen once it has closed
    assert engine.series(d, {"ind": "PRICE", "symbol": REF, "tf": 15}, {}) == [None, None, 30.0, 30.0, 30.0, 31.0]
    # transforms run on ITS candles: change over 1 of its bars, not of ours
    assert engine.series(d, {"ind": "PRICE", "symbol": REF, "xf": "change", "xn": 1}, {})[1:] == [1.0] * 5
    # a candle it never printed: our bar sees its last closed one
    gap = bars([10.0, 11, 12, 13, 14, 15])
    for k in ("t", "o", "h", "l", "c", "v"):
        del gap[k][2]
    d["xs"] = {REF: gap}
    assert engine.series(d, {"ind": "PRICE", "symbol": REF}, {}) == [10.0, 11, 11, 13, 14, 15]
    # stock / index ratio: own operand and the other symbol's, combined
    ratio = engine.series(d, {"math": "/", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": REF}}, {})
    assert ratio[0] == 10.0 and ratio[-1] == pytest.approx(100 / 15)
    with pytest.raises(ValueError, match="no candles loaded"):
        engine.series(bars([1.0, 2]), {"ind": "PRICE", "symbol": REF}, {})


def test_symbol_operands_are_validated():
    s = _ref_spec()
    assert engine.ref_frames(s) == {REF: {5}}
    two_tf = _cross_spec(entry_long={"logic": "AND", "conditions": [
        {"left": {"ind": "PRICE", "symbol": "nse:nifty50-index "}, "op": ">",
         "right": {"ind": "EMA", "params": {"period": 20}, "symbol": REF, "tf": 15}}]})
    assert two_tf["entry_long"]["conditions"][0]["left"]["symbol"] == REF        # stripped, upper-cased
    assert engine.ref_frames(two_tf) == {REF: {5, 15}}
    assert engine.cond_timeframes(two_tf) == set()      # the 15m candles are the index's, not ours
    bad = [
        ({"ind": "VIX", "symbol": REF}, "can't read another symbol"),
        ({"ind": "DELTA", "symbol": REF}, "can't read another symbol"),
        ({"value": 3, "symbol": REF}, "a number has no symbol"),
        ({"math": "-", "a": {"ind": "PRICE"}, "b": {"ind": "EMA"}, "symbol": REF}, "no symbol of its own"),
    ]
    for left, msg in bad:
        with pytest.raises(ValueError, match=msg):
            _cross_spec(entry_long={"logic": "AND", "conditions": [{"left": left, "op": ">", "right": {"value": 1}}]})
    many = [{"left": {"ind": "PRICE", "symbol": f"NSE:S{k}-EQ"}, "op": ">", "right": {"value": 1}} for k in range(6)]
    with pytest.raises(ValueError, match="at most 5 other symbols"):
        _cross_spec(entry_long={"logic": "AND", "conditions": many})
    assert len(engine.ref_frames(_cross_spec(entry_long={"logic": "AND", "conditions": many[:5]}))) == 5
    assert not any("symbol" in o for o in engine._walk_operands(_cross_spec()))   # absent = the spec it always was


def test_backtest_trades_the_stock_on_the_index_condition():
    own = bars([100.0, 100, 100, 100, 100, 100, 100, 100], spread=0)
    idx = bars([100.0, 101, 102, 106, 108, 104, 102, 101], spread=0)
    own["xs"] = {REF: idx}
    tr = engine.run(_ref_spec(), {"NSE:TEST-EQ": own})["trades"]
    assert len(tr) == 1
    assert tr[0]["entry_t"] == own["t"][4]                 # the index crossed on bar 3's close: filled at bar 4's open
    assert tr[0]["exit_t"] == own["t"][7] and tr[0]["reason"] == "SIGNAL"   # crossed below 103 on bar 6


def test_assemble_loads_each_other_symbol_once_and_shares_it(monkeypatch):
    calls = []

    async def fake_ensure(fy, start, end):
        return {"symbol": fy, "key": data.store_key(fy), "note": None, "coverage": None}

    def fake_frames(key, tfs, start, end, con=None):     # every timeframe of a symbol: one read
        calls.append((key, tuple(tfs)))
        return None if key == "MISSING-INDEX" else [bars([100.0 + i for i in range(80)], tf=tf * 60, start=DAY0) for tf in tfs]

    monkeypatch.setattr(data, "ensure", fake_ensure)
    monkeypatch.setattr(data, "load_frames", fake_frames)
    spec = _ref_spec(symbols=["NSE:AAA-EQ", "NSE:BBB-EQ"])
    out, notes, _ = asyncio.run(data.assemble(spec, DAY0, DAY0 + 3600))
    assert set(out) == {"NSE:AAA-EQ", "NSE:BBB-EQ"} and not notes
    assert out["NSE:AAA-EQ"]["xs"][REF] is out["NSE:BBB-EQ"]["xs"][REF]
    assert [c for c in calls if c[0] == "NIFTY50-INDEX"] == [("NIFTY50-INDEX", (5,))]
    gone = _ref_spec()
    gone["entry_long"]["conditions"][0]["left"]["symbol"] = "NSE:MISSING-INDEX"
    with pytest.raises(ValueError, match="MISSING-INDEX .*no candles"):
        asyncio.run(data.assemble(gone, DAY0, DAY0 + 3600))


def test_shared_bars_fetch_once_per_closed_candle(monkeypatch):
    fetched = []
    published = {"last": DAY0 + 300}                   # newest candle Fyers has published (start time)

    async def fake_recent(fy, tf, bars=400, now=None):
        fetched.append(now)
        n = (published["last"] - DAY0) // 300 + 1
        return globals()["bars"]([100.0] * n)

    monkeypatch.setattr(data, "recent_bars", fake_recent)
    monkeypatch.setattr(data, "_shared", {})
    assert data.last_closed(5, DAY0 + 600 + 3) == DAY0 + 300       # 09:25:03 -> the 09:20 candle
    assert data.last_closed(1440, DAY0) == DAY0 - 33300 - 86400    # before the close: yesterday
    now = DAY0 + 600 + 3
    asyncio.run(data.shared_bars(REF, 5, 50, now))
    asyncio.run(data.shared_bars(REF, 5, 50, now + 4))             # same closed candle: no new call
    assert len(fetched) == 1
    asyncio.run(data.shared_bars(REF, 5, 50, now + 300))           # 09:25 closed, not published yet …
    asyncio.run(data.shared_bars(REF, 5, 50, now + 304))           # … so it asks again
    assert len(fetched) == 3
    published["last"] = DAY0 + 600
    asyncio.run(data.shared_bars(REF, 5, 50, now + 308))
    asyncio.run(data.shared_bars(REF, 5, 50, now + 312))           # now it has it: reused
    assert len(fetched) == 4


def test_runner_waits_for_the_other_symbols_candle(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="t", spec=_ref_spec(), enabled=True, mode="paper"))
        db.commit()
    hold = {"ref": True}                               # Fyers hasn't published the index's newest candle

    async def fake_bars(sym, tf, bars=400, now=None):
        closes = [100.0, 101, 102, 106] if sym == REF else [100.0] * 4
        d = globals()["bars"](closes, spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now and not (sym == REF and hold["ref"] and i == 3)]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}

    async def fake_ltp(symbols):
        return {s.upper(): 100.0 for s in symbols}

    monkeypatch.setattr(rn.data, "recent_bars", fake_bars)
    monkeypatch.setattr(rn.data, "_shared", {})
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    t_close = DAY0 + 4 * 300 + 4                       # 09:35:04: bar 3 just closed
    asyncio.run(r.tick(t_close))
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).count() == 0        # waited instead of deciding on the index's old candle
    hold["ref"] = False
    asyncio.run(r.tick(t_close + 5))
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert (t.status, t.side, t.symbol) == ("open", "BUY", "NSE:TEST-EQ")


def test_runner_decides_on_the_last_closed_candle_once_the_wait_is_over(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="t", spec=_ref_spec(), enabled=True, mode="paper"))
        db.commit()

    async def fake_bars(sym, tf, bars=400, now=None):  # the index never prints its 09:30 candle
        closes = [100.0, 101, 102, 106] if sym == REF else [100.0] * 4
        d = globals()["bars"](closes, spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now and not (sym == REF and i == 3)]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}

    async def fake_ltp(symbols):
        return {s.upper(): 100.0 for s in symbols}

    monkeypatch.setattr(rn.data, "recent_bars", fake_bars)
    monkeypatch.setattr(rn.data, "_shared", {})
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    bar3 = DAY0 + 3 * 300
    asyncio.run(r.tick(bar3 + 300 + rn.REF_WAIT_S + 1))
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).count() == 0        # 102 is no cross above 105: decided, nothing to do
    s_id = next(iter(r._names))
    assert r._last_bar[(s_id, "NSE:TEST-EQ")] == bar3  # the bar is marked done, not polled all day


def test_api_resolves_operand_symbols_and_lists_the_exceptions(client):
    spec = _ref_spec()
    spec["entry_long"]["conditions"][0]["left"]["symbol"] = "nse:nifty50-index"
    r = client.post("/api/algo/validate", json={"spec": spec})
    assert r.status_code == 200
    assert r.json()["spec"]["entry_long"]["conditions"][0]["left"]["symbol"] == REF
    cat = client.get("/api/algo/indicators").json()
    assert {"VIX", "DTE", "TIME", "DELTA"} <= set(cat["no_symbol"])
    bad = _cross_spec()
    bad["entry_long"]["conditions"][0]["left"] = {"ind": "VIX", "symbol": REF}
    assert client.post("/api/algo/validate", json={"spec": bad}).status_code == 422


# ---- A2: every try counts toward the Deflated Sharpe -------------------------

def _days_bars(n_days: int, seed: int = 5):
    """5-minute candles over n_days sessions: a seeded random walk."""
    import random

    rnd, px, t, c = random.Random(seed), 100.0, [], []
    day0 = DAY0
    for k in range(n_days):
        for i in range(75):
            px *= 1 + rnd.gauss(0, 0.002)
            t.append(day0 + k * 86400 + i * 300)
            c.append(px)
    d = bars(c)
    d["t"] = t
    return d


def _ema_spec(**kw):
    """Long when price crosses above its 20-candle EMA: trades often on a random walk."""
    spec = _cross_spec(exit_long=None, target={"type": "pct", "value": 0.4}, stop_loss={"type": "pct", "value": 0.4}, **kw)
    spec["entry_long"]["conditions"][0]["right"] = {"ind": "EMA", "params": {"period": 20}}
    return spec


def test_more_tries_deflate_the_sharpe():
    d = _days_bars(30)
    # a modest edge: +300 / -150 on alternating days, one exit a day
    trades = [{"exit_t": DAY0 + k * 86400 + 3600, "net": 300.0 if k % 2 else -150.0} for k in range(30)]
    one = engine.trial_dsr(trades, {"X": d}, 100000, 1)
    many = engine.trial_dsr(trades, {"X": d}, 100000, 200)
    assert one["n_trials"] == 1 and many["n_trials"] == 200
    assert many["benchmark_sharpe"] > one["benchmark_sharpe"] == 0
    assert many["dsr"] < one["dsr"]                       # the same result means less after 200 tries
    assert engine.trial_dsr(trades, {"X": bars([100.0] * 20)}, 100000, 5) is None   # < 10 days


def _fake_assemble(monkeypatch):
    from app.api import algo as api

    async def fake(spec, start, end):
        return {s: _days_bars(30) for s in spec["symbols"]}, [], DAY0

    monkeypatch.setattr(api.data, "assemble", fake)


def test_backtests_and_sweeps_count_on_the_strategy_or_the_draft(client, monkeypatch, isolated_db):
    from app.db import session as dbs
    from app.db.models import AlgoStrategy

    _fake_assemble(monkeypatch)
    spec = _ema_spec()
    rng = {"start": "2025-01-01", "end": "2025-02-15"}
    s = client.post("/api/algo/strategies", json={"name": "rs", "spec": spec, "trials": 3}).json()
    assert s["trials"] == 3                                   # a draft's tries come along on save
    with dbs.SessionLocal() as db:
        stamp = db.get(AlgoStrategy, s["id"]).updated_at
    r = client.post("/api/algo/backtest", json={"spec": spec, **rng, "strategy_id": s["id"]}).json()
    assert r["trials"] == 4 and r["stats"]["deflated_sharpe"]["n_trials"] == 4
    grid = [{"path": "target.value", "values": [0.3, 0.4, 0.5]}]
    o = client.post("/api/algo/optimize", json={"spec": spec, **rng, "grid": grid, "min_trades": 1,
                                                "strategy_id": s["id"]}).json()
    assert o["trials"] == 7 and o["overfitting"]["deflated_sharpe"]["n_trials"] == 7
    with dbs.SessionLocal() as db:
        row = db.get(AlgoStrategy, s["id"])
        assert row.trials == 7 and row.updated_at == stamp     # counting is not an edit
    assert client.get("/api/algo/strategies").json()["strategies"][0]["trials"] == 7
    # an unsaved draft carries its own count
    assert client.post("/api/algo/backtest", json={"spec": spec, **rng, "trials": 9}).json()["trials"] == 10
    assert client.post("/api/algo/backtest", json={"spec": spec, **rng}).json()["trials"] == 1


# ---- A3: rank & pick the best N when several symbols fire on one candle -------

def _three(rank=None, **kw):
    s = {"symbols": ["NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ"], "exit_long": None,
         "portfolio": {"capital": 100000, "leverage": 1, "max_positions": kw.pop("max_positions", 10), "compounding": False}}
    if rank:
        s["rank"] = rank
    return _cross_spec(**s, **kw)


def _crossing(last3: dict[str, float]):
    """Every symbol crosses above 105 on bar 3, each to its own level."""
    return {sym: bars([100.0, 101, 102, px, px, px], spread=0) for sym, px in last3.items()}


BY_PRICE = {"by": {"ind": "PRICE"}, "order": "desc", "top": 1}


def test_rank_lets_only_the_best_enter_and_fills_them_first():
    levels = {"NSE:A-EQ": 106.0, "NSE:B-EQ": 110.0, "NSE:C-EQ": 108.0}
    plain = engine.run(_three(), _crossing(levels))["trades"]
    assert sorted(x["symbol"] for x in plain) == ["NSE:A-EQ", "NSE:B-EQ", "NSE:C-EQ"]
    top1 = engine.run(_three(BY_PRICE), _crossing(levels))["trades"]
    assert [x["symbol"] for x in top1] == ["NSE:B-EQ"]                 # the strongest of the three
    # one free slot: without rank the first listed takes it, with rank the best does
    first = engine.run(_three(max_positions=1), _crossing(levels))["trades"]
    assert [x["symbol"] for x in first] == ["NSE:A-EQ"]
    best = engine.run(_three({**BY_PRICE, "top": 2}, max_positions=1), _crossing(levels))["trades"]
    assert [x["symbol"] for x in best] == ["NSE:B-EQ"]
    asc = engine.run(_three({**BY_PRICE, "order": "asc"}), _crossing(levels))["trades"]
    assert [x["symbol"] for x in asc] == ["NSE:A-EQ"]                  # lowest first


def test_shorts_are_ranked_the_other_way_round():
    falls = {sym: bars([100.0, 99, 98, px, px, px], spread=0)
             for sym, px in {"NSE:A-EQ": 94.0, "NSE:B-EQ": 90.0, "NSE:C-EQ": 92.0}.items()}
    spec = _three(BY_PRICE, direction="short", entry_short={"logic": "AND", "conditions": [
        {"left": {"ind": "PRICE"}, "op": "crosses_below", "right": {"value": 95}}]}, exit_short=None)
    assert [x["symbol"] for x in engine.run(spec, falls)["trades"]] == ["NSE:B-EQ"]   # desc: short the weakest


def test_rank_is_validated_and_absent_by_default():
    assert "rank" not in _cross_spec()
    s = _three({"by": {"ind": "ROC", "params": {"period": 20}, "xf": "zscore", "xn": 30}, "top": 3})
    assert s["rank"] == {"by": {"ind": "ROC", "params": {"period": 20}, "xf": "zscore", "xn": 30}, "order": "desc", "top": 3}
    assert engine.warmup_bars(s) >= 3 * 30                 # what entries are ranked by warms up too
    for bad, msg in [({"by": {"value": 3}}, "rank is"), ({"by": {"ind": "PRICE"}, "order": "up"}, "rank order"),
                     ({"by": {"ind": "PRICE"}, "top": 51}, "at most 50"), ({"by": {"ind": "NOPE"}}, "unknown indicator")]:
        with pytest.raises(ValueError, match=msg):
            _three(bad)
    with pytest.raises(ValueError, match="time candles"):
        _three(BY_PRICE, bars={"type": "volume", "per_day": 50})
    rs = _three({"by": {"math": "/", "a": {"ind": "PRICE"}, "b": {"ind": "PRICE", "symbol": REF}}})
    assert engine.ref_frames(rs) == {REF: {5}}             # ranking by strength vs the index loads the index


def test_runner_ranks_every_symbols_candle_before_entering(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _cross_spec(symbols=["NSE:A-EQ", "NSE:B-EQ"], exit_long=None, rank=BY_PRICE)
    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="r", spec=spec, enabled=True, mode="paper"))
        db.commit()
    hold = {"A": True}                                 # A's newest candle isn't published yet

    async def fake_bars(sym, tf, bars=400, now=None):
        px = 106.0 if sym == "NSE:A-EQ" else 110.0
        d = globals()["bars"]([100.0, 101, 102, px], spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now and not (sym == "NSE:A-EQ" and hold["A"] and i == 3)]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}

    async def fake_ltp(symbols):
        return {s.upper(): 100.0 for s in symbols}

    monkeypatch.setattr(rn.data, "recent_bars", fake_bars)
    monkeypatch.setattr(rn.data, "_shared", {})
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    t_close = DAY0 + 4 * 300 + 4
    asyncio.run(r.tick(t_close))
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).count() == 0        # B was ready, but ranking waits for A's candle
    hold["A"] = False
    asyncio.run(r.tick(t_close + 5))
    with dbs.SessionLocal() as db:
        assert [t.symbol for t in db.query(AlgoTrade).all()] == ["NSE:B-EQ"]


# ---- A4: live vs backtest drift ------------------------------------------------

def test_match_trades_pairs_the_nearest_same_symbol_and_side():
    live = [{"symbol": "A", "side": "BUY", "entry_t": 1000}, {"symbol": "A", "side": "BUY", "entry_t": 5000},
            {"symbol": "B", "side": "SELL", "entry_t": 1000}]
    bt = [{"symbol": "A", "side": "BUY", "entry_t": 1150}, {"symbol": "A", "side": "BUY", "entry_t": 900},
          {"symbol": "B", "side": "BUY", "entry_t": 1000}, {"symbol": "C", "side": "BUY", "entry_t": 1000}]
    pairs, only_live, only_bt = engine.match_trades(live, bt, 420)
    assert [(a["entry_t"], b["entry_t"]) for a, b in pairs] == [(1000, 900)]      # nearest wins
    assert [(x["symbol"], x["entry_t"]) for x in only_live] == [("B", 1000), ("A", 5000)]   # other side / too far
    assert [(x["symbol"], x["entry_t"]) for x in only_bt] == [("A", 1150), ("B", 1000), ("C", 1000)]


def test_drift_lines_up_live_trades_with_the_backtest(client, monkeypatch, isolated_db):
    import copy
    import time as _t
    from datetime import datetime, timezone

    from app.api import algo as api
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoStrategyVersion, AlgoTrade

    base = int(_t.time()) - 2 * 86400
    day0 = base - (base + 19800) % 86400 + 33300           # 09:15 IST two days ago
    d = bars([100.0, 101, 102, 106, 108, 104, 102, 101, 100, 99, 100, 101, 102, 106, 107, 103, 102, 100, 99, 98],
             start=day0, spread=0)                        # the backtest: in at bar 4, out at 7; in at 14, out at 17

    async def fake(spec, start, end):
        return {"NSE:TEST-EQ": copy.deepcopy(d)}, [], day0

    monkeypatch.setattr(api.data, "assemble", fake)
    spec = _cross_spec()
    naive = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)  # noqa: E731
    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="d", spec=spec, version=1)
        db.add(s)
        db.flush()
        db.add(AlgoStrategyVersion(strategy_id=s.id, version=1, spec=spec))
        db.add(AlgoTrade(strategy_id=s.id, symbol="NSE:TEST-EQ", side="BUY", quantity=1, mode="paper", status="closed",
                         version=1, entry_at=naive(d["t"][4] + 5), entry_price=106.106, exit_at=naive(d["t"][7] + 3),
                         exit_price=101.0, exit_reason="SIGNAL", net_pnl=-5.2))
        db.add(AlgoTrade(strategy_id=s.id, symbol="NSE:TEST-EQ", side="BUY", quantity=1, mode="paper", status="rejected",
                         version=1, entry_at=naive(d["t"][10] + 5), entry_price=100.0, note="no live price"))
        db.commit()
        sid = s.id
    r = client.get(f"/api/algo/strategies/{sid}/drift?days=5").json()
    v = r["versions"][0]
    assert v["version"] == 1 and len(v["matched"]) == 1
    m = v["matched"][0]
    assert m["bt"]["entry"] == 106 and m["entry_gap_pct"] == pytest.approx(0.1) and m["same_reason"]
    assert [x["status"] for x in v["live_only"]] == ["rejected"]                  # live tried what the test never took
    assert [x["entry_t"] for x in v["bt_only"]] == [d["t"][14]]                  # … and missed what it did
    assert client.get(f"/api/algo/strategies/{sid}/drift?days=1").json()["versions"] == []   # nothing that recent
    assert client.get("/api/algo/strategies/999/drift").status_code == 404
