"""Algo engine: indicator math, no-lookahead fills, stops, square-off, costs."""
from __future__ import annotations

import pytest

from app.algo import engine
from app.algo import indicators as ind

DAY0 = 1735689600 - 19800 + 33300   # 2025-01-01 09:15 IST, epoch seconds


def bars(closes, tf=300, start=DAY0, spread=0.5):
    n = len(closes)
    opens = [closes[0]] + closes[:-1]
    return {"t": [start + i * tf for i in range(n)], "o": opens,
            "h": [max(o, c) + spread for o, c in zip(opens, closes)],
            "l": [min(o, c) - spread for o, c in zip(opens, closes)],
            "c": list(closes), "v": [1000] * n, "tf_s": tf}


def test_moving_averages_and_rsi():
    x = [float(i) for i in range(1, 11)]
    assert ind.sma(x, 3)[:3] == [None, None, 2.0] and ind.sma(x, 3)[-1] == 9.0
    e = ind.ema(x, 3)
    assert e[2] == 2.0 and e[3] == pytest.approx(0.5 * 4 + 0.5 * 2.0)
    assert ind.rsi(x, 5)[-1] == 100.0                 # only gains
    assert ind.highest(x, 3)[-1] == 10.0 and ind.lowest(x, 3)[-1] == 8.0
    assert ind.stdev([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0], 8)[-1] == pytest.approx(2.0)


def test_every_indicator_runs_and_keeps_length():
    d = bars([100 + (i % 17) - (i % 5) * 0.7 for i in range(300)])
    for name in ind.REGISTRY:
        out = ind.compute(d, name, {})
        for field, s in out.items():
            assert len(s) == 300, (name, field)


def _cross_spec(**kw):
    s = {"symbols": ["NSE:TEST-EQ"], "timeframe": 5, "direction": "long",
         "entry_long": {"logic": "AND", "conditions": [
             {"left": {"ind": "PRICE"}, "op": "crosses_above", "right": {"value": 105}}]},
         "exit_long": {"logic": "AND", "conditions": [
             {"left": {"ind": "PRICE"}, "op": "crosses_below", "right": {"value": 103}}]},
         "stop_loss": None, "target": None,
         "session": {"start": "09:15", "end": "15:00", "square_off": "15:15"},
         "costs": {"slippage_pct": 0, "charges": False}}
    s.update(kw)
    return engine.normalize(s)


def test_fill_is_next_bar_open_not_signal_close():
    closes = [100, 101, 102, 106, 108, 110, 104, 102, 101]
    d = bars(closes, spread=0)
    tr = engine.run(_cross_spec(), {"X": d})["trades"]
    assert len(tr) == 1
    t = tr[0]
    assert t["entry"] == d["o"][4] == 106        # signal on bar 3's close, filled at bar 4's open
    assert t["reason"] == "SIGNAL" and t["exit"] == d["o"][8] == 102
    assert t["gross"] == -4.0


def test_stop_loss_hits_inside_the_bar_before_target():
    closes = [100, 101, 106, 107, 100, 99]
    spec = _cross_spec(exit_long=None, stop_loss={"type": "points", "value": 2},
                       target={"type": "points", "value": 50})
    tr = engine.run(spec, {"X": bars(closes, spread=0)})["trades"]
    assert tr[0]["reason"] == "SL" and tr[0]["exit"] == pytest.approx(106 - 2)


def test_square_off_and_intraday_only():
    closes = [100] * 5 + [106] + [107] * 80        # entry then drift past 15:15
    spec = _cross_spec(exit_long=None)
    tr = engine.run(spec, {"X": bars(closes, spread=0)})["trades"]
    assert tr[0]["reason"] == "SQUARE_OFF"
    sod = (tr[0]["exit_t"] + 19800) % 86400
    assert sod == 15 * 3600 + 15 * 60


def test_charges_are_realistic():
    # ₹1L round trip intraday: brokerage 2x₹20, STT ₹25, exch ~₹5.94, GST, stamp ₹3
    ch = engine.charges(100000, 100000)
    assert 75 < ch < 85


def test_optimizer_ranks_and_validates():
    closes = [100 + (i % 20) for i in range(400)]
    spec = _cross_spec(exit_long=None, target={"type": "pct", "value": 1})
    res = engine.optimize(spec, {"X": bars(closes)}, [
        {"path": "entry_long.conditions.0.right.value", "values": [104, 108, 112]}], "net_pnl", min_trades=1)
    assert res["combos"] == 3 and res["ranked"]
    with pytest.raises(ValueError):
        engine.normalize({"symbols": ["X"], "timeframe": 7})


def test_runner_paper_session(monkeypatch, isolated_db):
    """Bar close -> paper entry at LTP; LTP through target -> exit; square-off flattens."""
    import asyncio

    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _cross_spec(exit_long=None, stop_loss={"type": "points", "value": 2},
                       target={"type": "points", "value": 3})
    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="t", spec=spec, enabled=True, mode="paper"))
        db.commit()

    closes = [100, 101, 102, 106]
    ltp = {"px": 106.5}

    async def fake_bars(sym, tf, bars=400, now=None):
        d = globals()["bars"](closes, spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}

    async def fake_ltp(symbols):
        return {s.upper(): ltp["px"] for s in symbols}

    monkeypatch.setattr(rn.data, "recent_bars", fake_bars)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    t_close = DAY0 + 4 * 300 + 4                     # 09:35:04 — bar 3 (close 106) just closed

    asyncio.run(r.tick(t_close))
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert (t.status, t.side, t.entry_price, t.stop_loss, t.target) == ("open", "BUY", 106.5, 104.5, 109.5)

    asyncio.run(r.tick(t_close + 5))                 # same bar again: no second entry
    ltp["px"] = 110.0
    asyncio.run(r.tick(t_close + 10))
    with dbs.SessionLocal() as db:
        rows = db.query(AlgoTrade).all()
        assert len(rows) == 1 and rows[0].status == "closed" and rows[0].exit_reason == "TARGET"
        assert rows[0].gross_pnl == 3.5 and rows[0].net_pnl < 3.5

    # a fresh position is flattened at the square-off time
    with dbs.SessionLocal() as db:
        db.add(AlgoTrade(strategy_id=rows[0].strategy_id, symbol="NSE:TEST-EQ", side="SELL", quantity=1,
                         mode="paper", status="open", entry_price=108.0,
                         entry_at=rn._utc(t_close + 60).replace(tzinfo=None)))
        db.commit()
    asyncio.run(r.tick(DAY0 + 6 * 3600 + 60))         # 15:16 IST
    with dbs.SessionLocal() as db:
        last = db.query(AlgoTrade).order_by(AlgoTrade.id.desc()).first()
        assert last.status == "closed" and last.exit_reason == "SQUARE_OFF"


def _nifty(closes, tf=300):
    d = bars(closes, tf=tf, spread=5)
    d["meta"] = {"symbol": "NSE:NIFTY50-INDEX", "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
    return d


def _opt_spec(**kw):
    s = {"symbols": ["NSE:NIFTY50-INDEX"], "timeframe": 5, "direction": "long",
         "entry_long": {"logic": "AND", "conditions": [
             {"left": {"ind": "PRICE"}, "op": "crosses_above", "right": {"value": 24550}}]},
         "instrument": {"type": "option", "expiry_kind": "weekly", "iv": {"source": "fixed", "value": 14}},
         "stop_loss": None, "target": None, "sizing": {"mode": "lots", "value": 2},
         "session": {"start": "09:15", "end": "15:00", "square_off": "15:15"},
         "portfolio": {"capital": 1_000_000, "leverage": 8},
         "costs": {"slippage_pct": 0, "charges": True}}
    s.update(kw)
    return engine.normalize(s)


def test_option_legs_priced_and_sized_in_lots():
    closes = [24500, 24520, 24540, 24560, 24600, 24650, 24700] + [24700] * 10
    r = engine.run(_opt_spec(), {"N": _nifty(closes)})
    t = r["trades"][0]
    leg = t["legs"][0]
    assert leg["label"] == "NIFTY 02JAN25 24550 CE" and leg["qty"] == 130 and leg["side"] == "BUY"  # ATM of the 24560 fill, weekly, 2 lots
    assert 0 < leg["entry"] < 400 and leg["exit"] > leg["entry"]                              # the call gained
    assert t["charges"] > 40                                                                  # ₹20 x 2 orders + STT etc.


def test_short_straddle_mtm_stop():
    closes = [24500, 24520, 24540, 24560] + [24560 + 60 * k for k in range(1, 12)]
    spec = _opt_spec(instrument={"type": "option", "expiry_kind": "weekly", "iv": {"source": "fixed", "value": 14},
                                 "legs_long": [{"right": "CE", "action": "SELL", "strike": "ATM", "lots": 1},
                                               {"right": "PE", "action": "SELL", "strike": "ATM", "lots": 1}]},
                     mtm={"stop": 5000})
    t = engine.run(spec, {"N": _nifty(closes)})["trades"][0]
    assert t["reason"] == "MTM_SL" and -6500 < t["gross"] <= -4900 and len(t["legs"]) == 2


def test_multi_timeframe_sees_only_closed_bars():
    base = bars([float(i) for i in range(1, 13)], tf=300)                 # 12 x 5m from 09:15
    htf = bars([10.0, 20.0, 30.0, 40.0], tf=900)                          # 4 x 15m
    base["tf_min"], base["htf"] = 5, {15: htf}
    s = engine.series(base, {"ind": "PRICE", "params": {}, "tf": 15}, {})
    # base bar i closes at 09:20 + 5i; the first 15m bar closes at 09:30 (base index 2)
    assert s[:2] == [None, None] and s[2] == 10.0 and s[4] == 10.0 and s[5] == 20.0


def test_portfolio_max_positions_and_daily_halt():
    closes = [100, 101, 102, 106, 108, 110, 104, 102, 101]
    spec = _cross_spec(exit_long=None, portfolio={"capital": 100000, "max_positions": 1})
    r = engine.run(spec, {"A": bars(closes, spread=0), "B": bars(closes, spread=0)})
    assert len(r["trades"]) == 1 and r["stats"]["skipped"]["max_positions"] == 1
    down = [100, 101, 102, 106, 103, 100, 97, 94, 92, 90]
    spec = _cross_spec(exit_long=None, daily={"max_loss": 5}, sizing={"mode": "qty", "value": 1})
    t = engine.run(spec, {"A": bars(down, spread=0)})["trades"][0]
    assert t["reason"] == "DAILY_MAX_LOSS"


def test_expiry_calendar_and_strikes():
    from datetime import datetime

    from app.algo import fno

    ts = lambda s: datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=fno.IST).timestamp()  # noqa: E731
    lab = lambda e: datetime.fromtimestamp(e, fno.IST).strftime("%Y-%m-%d")  # noqa: E731
    assert lab(fno.expiry_after("NIFTY", "NSE", "weekly", "current", ts("2025-08-26 10:00"))) == "2025-08-28"  # Thu era
    assert lab(fno.expiry_after("NIFTY", "NSE", "weekly", "current", ts("2025-09-01 10:00"))) == "2025-09-02"  # Tue era
    assert lab(fno.expiry_after("BANKNIFTY", "NSE", "weekly", "current", ts("2026-10-05 10:00"))) == "2026-10-27"
    assert lab(fno.expiry_after("NIFTY", "NSE", "weekly", "current", ts("2026-10-06 15:45"))) == "2026-10-13"
    assert fno.pick_strike(24537, 50, "CE", "ITM", 2) == 24450 and fno.pick_strike(24537, 50, "PE", "OTM", 1) == 24500


def test_live_legs_from_chain(monkeypatch):
    import asyncio

    from app.algo import runner as rn

    chain = {"spot": 24537, "expiries": [{"ts": "1791000000"}, {"ts": "1791600000"}, {"ts": "1793095800"}],
             "strikes": [{"strike": k, "ce": {"symbol": f"NSE:NIFTYX{k}CE", "ltp": 100 + (24550 - k) / 2},
                          "pe": {"symbol": f"NSE:NIFTYX{k}PE", "ltp": 100 - (24550 - k) / 2}}
                         for k in range(24300, 24850, 50)]}

    class B:
        async def get_option_chain(self, sym, strikecount=10, timestamp=""):
            return chain

    monkeypatch.setattr(rn.data, "_backend", lambda: B())
    spec = _opt_spec(instrument={"type": "option", "legs_long": [
        {"right": "CE", "action": "BUY", "strike": "OTM", "steps": 2, "lots": 1},
        {"right": "PE", "action": "SELL", "strike": "PREMIUM", "premium": 80, "lots": 1}]})
    legs = asyncio.run(rn.build_legs(spec, "NSE:NIFTY50-INDEX", "BUY", 24537, 1790000000))
    assert legs[0]["symbol"] == "NSE:NIFTYX24650CE" and legs[0]["act"] == 1
    assert legs[1]["symbol"] == "NSE:NIFTYX24500PE" and legs[1]["act"] == -1
    assert rn._pick_expiry([1791000000, 1791600000, 1793095800], "monthly", "BANKNIFTY", "current") == 1793095800
