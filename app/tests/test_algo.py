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


def test_runner_never_enters_without_its_stop(monkeypatch, isolated_db):
    """A 2 x ATR(200) stop on a symbol with 4 candles of history: no ATR, so no stop —
    the runner skips the entry instead of trading without it."""
    import asyncio

    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _cross_spec(exit_long=None, stop_loss={"type": "atr", "value": 2, "atr_period": 200})
    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="t", spec=spec, enabled=True, mode="paper"))
        db.commit()

    async def fake_bars(sym, tf, bars=400, now=None):
        d = globals()["bars"]([100, 101, 102, 106], spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}

    async def fake_ltp(symbols):
        return {s.upper(): 106.5 for s in symbols}

    monkeypatch.setattr(rn.data, "recent_bars", fake_bars)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    asyncio.run(r.tick(DAY0 + 4 * 300 + 4))          # bar 3 (close 106 > 105) just closed
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).count() == 0
    assert any("stop can't be set" in e["msg"] for e in r.events)


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


def test_protective_put_fut_leg_and_hedge_switch():
    """A FUT leg sits beside option legs (protective put); lots 0 switches a
    leg off, so the optimiser can compare hedged against unhedged."""
    closes = [24500, 24520, 24540, 24560] + [24560 - 60 * k for k in range(1, 8)]
    legs = lambda pe_lots: [{"right": "FUT", "action": "BUY", "lots": 1},  # noqa: E731
                            {"right": "PE", "action": "BUY", "strike": "ATM", "lots": pe_lots}]
    spec = lambda pe_lots: _opt_spec(instrument={"type": "option", "expiry_kind": "weekly", "levels_on": "underlying",  # noqa: E731
                                                 "iv": {"source": "fixed", "value": 14}, "legs_long": legs(pe_lots)})
    hedged = engine.run(spec(1), {"N": _nifty(closes)})["trades"][0]
    naked = engine.run(spec(0), {"N": _nifty(closes)})["trades"][0]
    assert [lg["label"].split()[1] for lg in hedged["legs"]] == ["FUT", "02JAN25"] and len(naked["legs"]) == 1
    assert naked["gross"] < hedged["gross"] < 0                     # the put paid back part of the fall
    data = {"N": _nifty(closes)}
    r = engine.optimize(spec(1), data, [{"path": "instrument.legs_long.1.lots", "values": [0, 1]}], min_trades=1)
    assert r["combos"] == 2 and len(r["ranked"]) == 2
    with pytest.raises(ValueError, match="at least one leg with lots"):
        _opt_spec(instrument={"type": "option", "legs_long": [{"right": "PE", "action": "BUY", "lots": 0}]})


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


def test_live_fut_leg_beside_options(monkeypatch):
    import asyncio

    from app.algo import fno
    from app.algo import runner as rn

    chain = {"spot": 24537, "expiries": [{"ts": "1791000000"}],
             "strikes": [{"strike": k, "pe": {"symbol": f"NSE:NIFTYX{k}PE", "ltp": 90}} for k in range(24400, 24700, 50)]}

    class B:
        async def get_option_chain(self, sym, strikecount=10, timestamp=""):
            return chain

    async def ltp(syms):
        return {s: 24560.0 for s in syms}

    monkeypatch.setattr(rn.data, "_backend", lambda: B())
    monkeypatch.setattr(rn, "_ltp", ltp)
    monkeypatch.setattr(fno, "future_symbol", lambda name, which, now=None: "NSE:NIFTY26OCTFUT")
    monkeypatch.setattr(fno, "contract_lot", lambda sym, now=None: 65)
    spec = _opt_spec(instrument={"type": "option", "levels_on": "underlying", "legs_long": [
        {"right": "FUT", "action": "BUY", "lots": 1}, {"right": "PE", "action": "BUY", "strike": "ATM", "lots": 1},
        {"right": "CE", "action": "SELL", "strike": "OTM", "steps": 2, "lots": 0}]})
    legs = asyncio.run(rn.build_legs(spec, "NSE:NIFTY50-INDEX", "BUY", 24537, 1790000000))
    assert [(lg["symbol"], lg["kind"], lg["act"], lg["per_set"]) for lg in legs] == [
        ("NSE:NIFTY26OCTFUT", "FUT", 1, 65), ("NSE:NIFTYX24550PE", "PE", 1, 65)]
    assert legs[0]["price"] == 24560.0


def test_runner_paper_straddle_mtm(monkeypatch, isolated_db):
    """Runner, option legs: entry on bar close builds both legs at live
    premiums; a premium spike trips the MTM stop and both legs are booked."""
    import asyncio

    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _opt_spec(direction="long", instrument={"type": "option", "levels_on": "underlying",
                     "legs_long": [{"right": "CE", "action": "SELL", "strike": "ATM", "lots": 1},
                                   {"right": "PE", "action": "SELL", "strike": "ATM", "lots": 1}]},
                     mtm={"stop": 2000}, sizing={"mode": "lots", "value": 1})
    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="straddle", spec=spec, enabled=True, mode="paper"))
        db.commit()
    px = {"NSE:NIFTY50-INDEX": 24570.0, "NSE:NIFTYCE": 100.0, "NSE:NIFTYPE": 90.0}

    async def fake_bundle(spec_, sym, now):
        d = bars([24500, 24520, 24540, 24560], spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now]
        d = {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}
        d.update(tf_min=5, htf={}, meta={"symbol": sym, "name": "NIFTY", "exch": "NSE", "lot": 65})
        return d

    async def fake_legs(spec_, sym, side, u, now):
        return [{"symbol": "NSE:NIFTYCE", "kind": "CE", "act": -1, "per_set": 65, "label": "CE", "price": px["NSE:NIFTYCE"]},
                {"symbol": "NSE:NIFTYPE", "kind": "PE", "act": -1, "per_set": 65, "label": "PE", "price": px["NSE:NIFTYPE"]}]

    async def fake_ltp(symbols):
        return {s.upper(): px[s.upper()] for s in symbols if s.upper() in px}

    monkeypatch.setattr(rn.data, "live_bundle", fake_bundle)
    monkeypatch.setattr(rn, "build_legs", fake_legs)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    t_close = DAY0 + 4 * 300 + 4
    asyncio.run(r.tick(t_close))
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert t.status == "open" and len(t.legs) == 2 and t.legs[0]["qty"] == 65 and t.ref == "u"
    px["NSE:NIFTYCE"] = 135.0                       # CE +35, PE -2: MTM = -(35 - 2) x 65 = -2145
    px["NSE:NIFTYPE"] = 88.0
    asyncio.run(r.tick(t_close + 5))
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert t.status == "closed" and t.exit_reason == "MTM_SL" and t.gross_pnl == -2145.0
        assert t.charges > 80 and all(lg["exit"] for lg in t.legs)


def test_session_times_can_be_switched_off():
    closes = [100] * 5 + [106] + [107] * 80
    spec = _cross_spec(exit_long=None, session={"start": None, "end": None, "square_off": None})
    t = engine.run(spec, {"X": bars(closes, spread=0)})["trades"][0]
    assert t["reason"] == "END"          # square-off off = carry forward: never time-squared


def test_runner_carries_when_square_off_is_off(isolated_db):
    """Square-off switched off: no SQUARE_OFF after 15:30, no STALE_SESSION
    close the next morning — and live orders go out as NRML / CNC, not MIS."""
    import asyncio

    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _cross_spec(exit_long=None, session={"start": None, "end": None, "square_off": None})
    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="c", spec=spec, enabled=False, mode="paper")
        db.add(s)
        db.flush()
        db.add(AlgoTrade(strategy_id=s.id, symbol="NSE:TEST-EQ", side="BUY", quantity=1, mode="paper",
                         status="open", entry_price=100.0, entry_at=rn._utc(DAY0 + 3600).replace(tzinfo=None)))
        db.commit()
    r = rn.AlgoRunner()
    asyncio.run(r.tick(DAY0 + 6 * 3600 + 20 * 60))          # 15:35 IST, same day
    asyncio.run(r.tick(DAY0 + 86400 + 60))                  # next day 09:16
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).one().status == "open"
    norm = engine.normalize(spec)
    assert rn._product(norm, {"kind": "EQ"}) == "DELIVERY" and rn._product(norm, {"kind": "CE"}) == "MARGIN"
    assert rn._product(engine.normalize(_cross_spec()), {"kind": "CE"}) == "INTRADAY"


def test_pullback_and_breakout_entries():
    closes = [100, 101, 102, 106, 106.5, 105.2, 104.0, 104.5, 104.4, 104.3]
    pull = _cross_spec(exit_long=None, entry_order={"type": "pullback", "offset_pct": 1.5, "valid_bars": 5})
    t = engine.run(pull, {"X": bars(closes, spread=0)})["trades"][0]
    assert t["entry"] == round(106 * 0.985, 2)                 # filled at the trigger, not the next open
    never = _cross_spec(exit_long=None, entry_order={"type": "pullback", "offset_pct": 5, "valid_bars": 2})
    r = engine.run(never, {"X": bars(closes, spread=0)})
    assert not r["trades"] and r["stats"]["skipped"]["entry_expired"] == 1
    brk = _cross_spec(exit_long=None, entry_order={"type": "breakout", "offset_pct": 0.4, "valid_bars": 3})
    assert engine.run(brk, {"X": bars(closes, spread=0)})["trades"][0]["entry"] == round(106 * 1.004, 2)  # stop at trigger


def test_triggered_entry_cannot_take_target_on_its_own_bar():
    # Pullback buy at 104.41 fills in bar 5, whose high (106.5) came at its open —
    # before the dip. The target (105) must not be booked on that same bar.
    d = bars([100, 101, 102, 106, 106.5, 104.0, 104.2, 105.5], spread=0)
    spec = _cross_spec(exit_long=None, target={"type": "points", "value": 0.6},
                       entry_order={"type": "pullback", "offset_pct": 1.5, "valid_bars": 5})
    t = engine.run(spec, {"X": d})["trades"][0]
    assert t["entry"] == round(106 * 0.985, 2) and t["exit_t"] > d["t"][5]


def _two_days_15m(day1, day2):
    n = 25                                            # 15m candles per session, 09:15 .. 15:15
    d = bars(day1 + day2, tf=900, spread=0)
    d["t"] = [DAY0 + (k // n) * 86400 + (k % n) * 900 for k in range(2 * n)]
    return d


def test_daily_loss_limit_counts_only_that_days_pnl():
    # 15m candles, square-off 15:20: no candle starts at/after it, so day 1's position
    # is closed (EOD) at day 2's first candle. Its ₹1,000 loss is day 1's — it must
    # not trip day 2's ₹500 daily stop and block day 2's trade.
    day1 = [100] * 3 + [106] * 21 + [96]
    day2 = [96, 96, 97, 101, 106, 107, 108, 109] + [109] * 17
    spec = _cross_spec(timeframe=15, exit_long=None, sizing={"mode": "qty", "value": 100},
                       session={"start": "09:15", "end": "15:00", "square_off": "15:20"},
                       daily={"max_loss": 500, "max_profit": None})
    tr = engine.run(spec, {"X": _two_days_15m(day1, day2)})["trades"]
    assert [(x["reason"], (x["entry_t"] - DAY0) // 86400) for x in tr] == [("EOD", 0), ("END", 1)]
    # ... nor while that position is still waiting for its own first candle of day 2,
    # when another symbol's candle at the same time is processed first
    flat = _two_days_15m([100] * 25, day2)
    tr = engine.run(spec, {"Y": flat, "X": _two_days_15m(day1, [96] * 25)})["trades"]
    assert sorted((x["symbol"], x["reason"]) for x in tr) == [("X", "EOD"), ("Y", "END")]


def test_entry_order_lives_exactly_its_valid_bars():
    # Breakout trigger 106.53 (signal close 106 + 0.5%): bar 4 tops at 106.2, bar 5 reaches
    # it. Valid for 1 bar = bar 4 only (live arms it for one bar's time); 2 bars = filled on bar 5.
    d = bars([100, 101, 102, 106, 106.2, 107.5, 108, 108.2, 108.4], spread=0)
    one = engine.run(_cross_spec(exit_long=None, entry_order={"type": "breakout", "offset_pct": 0.5, "valid_bars": 1}),
                     {"X": d})
    assert one["trades"] == [] and one["stats"]["skipped"]["entry_expired"] == 1
    (t,) = engine.run(_cross_spec(exit_long=None, entry_order={"type": "breakout", "offset_pct": 0.5, "valid_bars": 2}),
                      {"X": d})["trades"]
    assert t["entry_t"] == d["t"][5] and t["entry"] == pytest.approx(106.53)


def test_warmup_covers_default_periods_level_atrs_and_rvol_sessions():
    assert engine.warmup_bars(_cross_spec()) == 3 * 14 + 5
    cloud = {"logic": "AND", "conditions": [{"left": {"ind": "PRICE"}, "op": ">",
                                             "right": {"ind": "ICHIMOKU", "field": "span_b"}}]}
    assert engine.warmup_bars(_cross_spec(entry_long=cloud)) == 3 * 52 + 5        # its default span_b 52 counts
    assert engine.warmup_bars(_cross_spec(stop_loss={"type": "atr", "value": 2, "atr_period": 200})) == 605
    assert engine.warmup_bars(_cross_spec(target={"type": "atr", "value": 2, "atr_period": 900})) == 1000  # capped at 1000 ...
    assert engine.warmup_bars(_cross_spec(target={"type": "atr", "value": 2, "atr_period": 1500})) == 1505  # ... never below it
    rv = _cross_spec(entry_long={"logic": "AND", "conditions": [
        {"left": {"ind": "RVOL", "params": {"days": 10}}, "op": ">", "right": {"value": 2}}]})
    assert engine.warmup_sessions(rv) == 11 and engine.warmup_bars(rv) == 47       # whole sessions, not 10 candles
    assert engine.history_bars(rv, 5) == 11 * 75 and engine.history_bars(rv, 1440) == 47


def test_no_entry_without_its_stop():
    # A 2 x ATR(200) stop with only 6 sessions (150 candles) of 15m history before the
    # first tradable day: until the ATR exists those entries are skipped, never stop-less.
    n = 25
    closes = [100 + (k % 25) * 0.3 for k in range(12 * n)]                       # crosses 105 every session
    d = bars(closes, tf=900, spread=0.3)
    d["t"] = [DAY0 + (k // n) * 86400 + (k % n) * 900 for k in range(len(closes))]
    spec = _cross_spec(timeframe=15, exit_long=None, stop_loss={"type": "atr", "value": 2, "atr_period": 200})
    r = engine.run(spec, {"X": d}, None, DAY0 + 6 * 86400 - 33300)
    assert r["stats"]["skipped"]["no_stop"] == 2
    assert r["trades"] and all(x["stop"] is not None for x in r["trades"])


def test_market_entry_can_take_its_target_on_its_own_bar():
    # A market entry fills at bar 4's OPEN, so all of bar 4 comes after the fill:
    # its high (109) passes the 108 target, which must be booked there — not
    # skipped until the price has collapsed into the stop.
    d = bars([100, 101, 102, 106, 107.5, 101, 100, 99, 98], spread=0)
    d["h"][4] = 109.0
    spec = _cross_spec(exit_long=None, target={"type": "points", "value": 2}, stop_loss={"type": "points", "value": 4})
    t = engine.run(spec, {"X": d})["trades"][0]
    assert (t["entry"], t["exit"], t["reason"]) == (106, 108, "TARGET") and t["exit_t"] == d["t"][4]


def test_mid_bar_entry_stop_fills_at_the_stop_not_the_bar_extreme():
    # Breakout trigger 106.53 fills inside bar 5, which OPENED at 104.5 — already
    # below the 1% stop (105.46). The stop is hit after the fill (the bar's low is
    # 103) and must fill at the stop, not at the low: the open-to-fill stretch
    # happened before the position existed.
    d = bars([100, 101, 102, 106, 104.5, 107.2, 107.5, 107.6, 107.7], spread=0)
    d["l"][4], d["h"][4] = 104.0, 106.4
    d["o"][5], d["l"][5], d["h"][5] = 104.5, 103.0, 107.5
    spec = _cross_spec(exit_long=None, stop_loss={"type": "pct", "value": 1.0},
                       entry_order={"type": "breakout", "offset_pct": 0.5, "valid_bars": 3})
    t = engine.run(spec, {"X": d})["trades"][0]
    assert t["entry"] == pytest.approx(106.53) and t["reason"] == "SL"
    assert t["exit"] == pytest.approx(106.53 * 0.99, abs=0.01)


def test_mid_bar_entry_does_not_count_the_high_before_its_fill():
    # Pullback buy fills at 104.41 inside bar 4, whose 107 high came BEFORE the dip.
    # That high must not arm the 1-point trailing stop (it would sit at 106, above
    # the entry) nor count as the trade's best excursion.
    d = bars([100, 101, 102, 106, 104.6, 105.2, 105.6, 106.0, 106.3], spread=0)
    d["h"][4], d["l"][4] = 107.0, 104.0
    d["h"][5], d["l"][5] = 105.3, 104.5
    spec = _cross_spec(exit_long=None, trailing={"type": "points", "value": 1.0},
                       entry_order={"type": "pullback", "offset_pct": 1.5, "valid_bars": 3})
    t = engine.run(spec, {"X": d})["trades"][0]
    assert t["entry"] == pytest.approx(104.41) and t["reason"] == "END" and t["exit"] == pytest.approx(106.3)
    assert t["mfe_pct"] < 2.0


def test_expiry_moves_back_off_a_holiday():
    from datetime import date, datetime

    from app.algo import fno

    ts = datetime(2026, 10, 5, 10, 0, tzinfo=fno.IST).timestamp()
    day = lambda d: (d - date(1970, 1, 1)).days  # noqa: E731
    traded = frozenset(day(date(2026, 10, k)) for k in (1, 2, 5, 7, 8, 9))     # Tue 6 Oct closed
    e = fno.expiry_after("NIFTY", "NSE", "weekly", "current", ts, traded)
    assert datetime.fromtimestamp(e, fno.IST).date() == date(2026, 10, 5)


def test_strategy_versions(client):
    base = {"symbols": ["NSE:SBIN-EQ"], "timeframe": 5, "direction": "long",
            "entry_long": {"logic": "AND", "conditions": [
                {"left": {"ind": "EMA", "params": {"period": 9}}, "op": "crosses_above",
                 "right": {"ind": "EMA", "params": {"period": 21}}}]}}
    sid = client.post("/api/algo/strategies", json={"name": "ver-test", "spec": base}).json()["id"]
    v2 = {**base, "timeframe": 15}
    v3 = {**base, "timeframe": 30}
    assert client.put(f"/api/algo/strategies/{sid}", json={"spec": v2}).json()["version"] == 2
    assert client.put(f"/api/algo/strategies/{sid}", json={"spec": v2}).json()["version"] == 2   # unchanged: no new version
    assert client.put(f"/api/algo/strategies/{sid}", json={"spec": v3}).json()["version"] == 3
    r = client.post(f"/api/algo/strategies/{sid}/versions/1/activate").json()
    assert r["version"] == 1 and r["spec"]["timeframe"] == 5 and r["versions"] == 3
    vs = client.get(f"/api/algo/strategies/{sid}/versions").json()
    assert [v["version"] for v in vs["versions"]] == [3, 2, 1] and vs["active"] == 1
    assert client.post(f"/api/algo/strategies/{sid}/versions/3/activate").json()["spec"]["timeframe"] == 30
    assert client.put(f"/api/algo/strategies/{sid}", json={"spec": {**base, "timeframe": 10}}).json()["version"] == 4
    client.delete(f"/api/algo/strategies/{sid}")


def test_preflight_runs_even_when_daily_report_breaks(monkeypatch):
    import asyncio

    from app.services import health_report as hr

    svc = hr.HealthReportService()
    calls = []

    async def boom():
        raise RuntimeError("database disk image is malformed")

    async def preflight(now=None):
        calls.append("preflight")
        svc._stop_event.set()
        return []

    published = []

    async def pub(ch, payload):
        published.append(payload.get("error"))
        return 1

    monkeypatch.setattr(svc, "_due", lambda now=None: True)
    monkeypatch.setattr(svc, "_preflight_due", lambda now=None: True)
    monkeypatch.setattr(svc, "send_now", boom)
    monkeypatch.setattr(svc, "run_preflight", preflight)
    monkeypatch.setattr(hr.event_bus, "publish", pub)
    asyncio.run(svc._run())
    assert calls == ["preflight"] and published == ["health_report_failed"]


def test_runner_pullback_entry_waits_for_trigger(monkeypatch, isolated_db):
    import asyncio

    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _cross_spec(exit_long=None, entry_order={"type": "pullback", "offset_pct": 1, "valid_bars": 3})
    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="pb", spec=spec, enabled=True, mode="paper", version=2))
        db.commit()
    ltp = {"px": 106.0}

    async def fake_bars(sym, tf, bars=400, now=None):
        d = globals()["bars"]([100, 101, 102, 106], spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}

    async def fake_ltp(symbols):
        return {s.upper(): ltp["px"] for s in symbols}

    monkeypatch.setattr(rn.data, "recent_bars", fake_bars)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    t0 = DAY0 + 4 * 300 + 4
    asyncio.run(r.tick(t0))                          # signal -> armed at 104.94, no trade yet
    ltp["px"] = 105.5
    asyncio.run(r.tick(t0 + 5))                      # not touched
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).count() == 0 and r._armed
    ltp["px"] = 104.9
    asyncio.run(r.tick(t0 + 10))                     # touched -> enters
    with dbs.SessionLocal() as db:
        t = db.query(AlgoTrade).one()
        assert t.entry_price == 104.9 and t.version == 2 and not r._armed


def test_quant_stats_monte_carlo_and_oos():
    closes = [100 + (i % 20) - (i % 7) * 0.5 for i in range(1500)]
    spec = _cross_spec(exit_long=None, stop_loss={"type": "pct", "value": 1}, target={"type": "pct", "value": 1.5})
    d = bars(closes, spread=0.4)
    split = d["t"][900]
    r = engine.run(spec, {"X": d}, oos_from=split)
    s = r["stats"]
    assert s["trades"] >= 10 and s["in_sample"]["trades"] + s["out_of_sample"]["trades"] == s["trades"]
    assert s["t_stat"] is not None and 0 < s["exposure_pct"] <= 100 and s["ulcer_index"] >= 0
    t = r["trades"][0]
    assert t["mae_pct"] <= 0 <= t["mfe_pct"] and t["r"] is not None       # excursions signed; 1R from the stop
    mc = engine.monte_carlo(r["trades"], 100000, runs=200)
    assert mc["net_p5"] <= mc["net_p50"] <= mc["net_p95"] and 0 <= mc["prob_loss_pct"] <= 100
    assert engine.monte_carlo(r["trades"][:5], 100000) is None              # too few trades to say anything
    assert r["per_symbol"]["X"]["buy_hold_pct"] is not None


def test_edge_cases_are_clean_errors_not_crashes():
    with pytest.raises(ValueError):            # constant that isn't a number
        _cross_spec(entry_long={"logic": "AND", "conditions": [
            {"left": {"ind": "PRICE"}, "op": ">", "right": {"value": "abc"}}]})
    with pytest.raises(ValueError):            # session inverted
        _cross_spec(session={"start": "14:00", "end": "10:00", "square_off": "15:15"})
    with pytest.raises(ValueError):            # multi-leg with levels on the premium
        _opt_spec(instrument={"type": "option", "legs_long": [
            {"right": "CE", "action": "SELL"}, {"right": "PE", "action": "SELL"}]}, stop_loss={"type": "pct", "value": 20})
    from app.algo import fno
    assert fno.pick_strike(30, 5, "PE", "OTM", 20) == 5                    # never a zero/negative strike
    flat = bars([100.0] * 60, spread=0)                                     # no movement at all
    for name in ind.REGISTRY:
        ind.compute(flat, name, {})                                         # no ZeroDivision anywhere
    tiny = engine.run(_cross_spec(portfolio={"capital": 50}), {"X": bars([100, 101, 102, 106, 108], spread=0)})
    assert tiny["trades"] == [] and tiny["stats"]["skipped"]["no_capital"] == 1
    assert engine.run(_cross_spec(), {"X": bars([100], spread=0)})["trades"] == []   # a single bar


def test_api_guards_heavy_jobs(client):
    spec = {"symbols": [f"NSE:S{i}-EQ" for i in range(40)], "timeframe": 1, "direction": "long",
            "entry_long": {"logic": "AND", "conditions": [{"left": {"ind": "PRICE"}, "op": ">", "right": {"value": 1}}]}}
    r = client.post("/api/algo/backtest", json={"spec": spec, "start": "2024-01-01", "end": "2026-01-01"})
    assert r.status_code == 422 and "too much data" in r.json()["detail"]
    r = client.post("/api/algo/backtest", json={"spec": {**spec, "symbols": ["NSE:SBIN-EQ"], "timeframe": 15},
                                                 "start": "2025-01-01", "end": "2025-06-01", "oos_pct": 95})
    assert r.status_code == 422 and "out-of-sample" in r.json()["detail"]


def _minutes(closes, vols=None, start=DAY0, days=1):
    """1-minute candles: `closes` per session, repeated over `days` sessions."""
    t, o, h, l, c, v = [], [], [], [], [], []
    for dd in range(days):
        prev = closes[0]
        for i, x in enumerate(closes):
            t.append(start + dd * 86400 + 60 * i)
            o.append(prev)
            h.append(max(prev, x) + 0.05)
            l.append(min(prev, x) - 0.05)
            c.append(x)
            v.append((vols[i] if vols else 100) * (dd + 1))
            prev = x
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": v, "tf_s": 60}


def test_orderflow_candles_from_minutes():
    from app.algo import orderflow as of

    m1 = _minutes([100 + i * 0.1 for i in range(30)])
    b = of.build_bars(m1, "time", 5)
    assert len(b["t"]) == 6 and b["o"][0] == 100 and b["c"][0] == round(100.4, 10) and b["v"][0] == 500
    assert all(abs(bu + se - v) < 1e-6 for bu, se, v in zip(b["buy_v"], b["sell_v"], b["v"]))
    assert all(x > 0 for x in b["delta"][2:])                     # a steady climb is net buying
    assert b["tc"][0] == DAY0 + 300 and b["last_complete"]
    vb = of.build_bars(_minutes([100] * 20, days=2), "volume", size=450)
    days = {(t + 19800) // 86400 for t in vb["t"]}
    assert len(days) == 2 and all(v >= 450 for v in vb["v"][:4])  # thresholds; nothing spans sessions
    assert of.build_bars(_minutes([100] * 7), "volume", size=450)["last_complete"] is False
    with pytest.raises(ValueError):
        of.bar_size(_minutes([100] * 30, vols=[0] * 30), "volume", 50)      # an index: no volume


def test_orderflow_indicators():
    from app.algo import orderflow as of

    # day 1 trades mostly at 100, day 2 doubles volume
    closes = [100.0] * 20 + [101.0] * 5 + [100.0] * 5
    vols = [500] * 20 + [50] * 5 + [500] * 5
    b = of.build_bars(_minutes(closes, vols, days=2), "time", 5)
    b["tf_min"] = 5
    vp = ind.compute(b, "VPROFILE", {})
    day2 = [i for i, t in enumerate(b["t"]) if t >= DAY0 + 86400]
    assert abs(vp["poc"][day2[-1]] - 100) < 0.2 and vp["val"][-1] <= vp["poc"][-1] <= vp["vah"][-1]
    assert vp["prev_poc"][day2[0]] is not None and vp["prev_poc"][0] is None
    rv = ind.compute(b, "RVOL", {"days": 5})["value"]
    assert rv[0] is None and abs(rv[day2[-1]] - 2.0) < 1e-6        # twice yesterday's volume by the same time
    cvd = ind.compute(b, "CVD", {})
    assert cvd["session"][day2[0]] == b["delta"][day2[0]]          # session CVD resets at the open
    plain = bars([100 + (i % 9) for i in range(80)])               # candles not built from minutes
    assert len(ind.compute(plain, "DELTA", {})["delta"]) == 80      # bar-level fallback still works


def test_volume_profile_never_reads_later_candles():
    # A later price (here a jump to 5x) must not change what an earlier candle saw:
    # neither the price bins nor when the value area was last recomputed.
    for tf in (60, 300):
        n = 375 * 60 // tf
        closes = [100 + (i % 13) * 0.4 for i in range(n)] + [500 + (i % 7) for i in range(n)]
        d = bars(closes, tf=tf)
        d["t"] = [DAY0 + (i // n) * 86400 + (i % n) * tf for i in range(2 * n)]   # two sessions
        full = ind.compute(d, "VPROFILE", {})
        for cut in (7, n // 2, n + 3):
            part = ind.compute({k: (v[:cut] if isinstance(v, list) else v) for k, v in d.items()}, "VPROFILE", {})
            for k, s in part.items():
                assert s == full[k][:cut], (tf, cut, k)


def test_volume_candle_strategy_backtests():
    from app.algo import orderflow as of

    closes = ([100 + i * 0.05 for i in range(200)] + [110 - i * 0.05 for i in range(175)])
    m1 = _minutes(closes, days=3)
    spec = _cross_spec(bars={"type": "volume", "per_day": 40}, exit_long=None,
                       entry_long={"logic": "AND", "conditions": [
                           {"left": {"ind": "DELTA", "field": "delta_pct"}, "op": ">", "right": {"value": 10}},
                           {"left": {"ind": "PRICE"}, "op": "crosses_above", "right": {"ind": "EMA", "params": {"period": 5}}}]},
                       stop_loss={"type": "pct", "value": 0.5}, target={"type": "pct", "value": 1})
    assert spec["timeframe"] == 1 and engine.needs_minutes(spec)
    d = of.build_bars(m1, "volume", size=of.bar_size(m1, "volume", 40))
    d["tf_min"] = 1
    r = engine.run(spec, {"X": d})
    assert r["trades"] and all(t["exit_t"] in d["tc"] or t["reason"] in ("SL", "TARGET") or t["exit_t"] in d["t"]
                               for t in r["trades"])


def test_live_minute_cache_fetches_only_new_minutes(monkeypatch):
    """Runner path for volume candles: first call loads history, later calls
    fetch only the minutes since the last one and drop the forming candle."""
    import asyncio

    from app.algo import data, orderflow

    data._m1_cache.clear()
    calls = []
    t0 = DAY0

    class B:
        async def get_history_range(self, fy, resolution, from_ts, to_ts):
            calls.append((from_ts, to_ts))
            return [[t, 100.0, 100.2, 99.8, 100.1, 100] for t in range(max(from_ts, t0) // 60 * 60, to_ts + 1, 60)
                    if t >= t0]

    monkeypatch.setattr(data, "_backend", lambda: B())
    now = t0 + 30 * 60 + 5                                  # 09:45:05
    a = asyncio.run(data.minute_history("NSE:X-EQ", 200, now))
    assert a["t"][-1] == t0 + 29 * 60                       # the 09:45 minute is still forming
    b = asyncio.run(data.minute_history("NSE:X-EQ", 200, now + 120))
    assert calls[-1][0] == t0 + 30 * 60 and b["t"][-1] == t0 + 31 * 60 and len(b["t"]) == 32
    vb = orderflow.complete_only(orderflow.build_bars(b, "volume", size=450), now + 120)
    assert all(v >= 450 for v in vb["v"])                   # forming volume candle dropped


def test_adv_from_daily_candles_unblocks_known_liquid_symbols():
    """Fyers quotes carry no ADV; it is derived from daily candles so
    REQUIRE_KNOWN_LIQUIDITY doesn't refuse every live order."""
    from app.risk.engine import _ADV_CACHE, adv_from_candles, cached_adv
    import time as _t

    candles = [[0, 1, 1, 1, 800.0, 2_000_000] for _ in range(30)]   # ₹160 cr / day
    assert abs(adv_from_candles(candles) - 160.0) < 1e-6
    assert adv_from_candles([]) is None
    _ADV_CACHE[("NSE:SBIN-EQ", int((_t.time() + 19800) // 86400))] = 160.0
    assert cached_adv("nse:sbin-eq") == 160.0


def test_units_caps_one_position_at_max_position_pct() -> None:
    s = _cross_spec(sizing={"mode": "pct_equity", "value": 50},
                    portfolio={"capital": 100000, "max_position_pct": 20})
    # 50% of 1L = 500 shares at ₹100, but one position may only block 20% → 200.
    assert engine.units(s, 100000, 100.0, None) == 200
    s["portfolio"]["max_position_pct"] = None
    assert engine.units(s, 100000, 100.0, None) == 500
    with pytest.raises(ValueError):
        _cross_spec(portfolio={"max_position_pct": -5})


def test_estimate_counts_minute_data_once_not_per_symbol() -> None:
    """46 symbols x 30 days at 15m with an order-flow indicator: minutes are
    folded per symbol and freed, so this is ~60k candles held, not ~900k."""
    from app.api.algo import MAX_BARS, _estimate_bars

    s = _cross_spec(symbols=[f"NSE:S{i}-EQ" for i in range(46)], timeframe=15,
                    entry_long={"logic": "AND", "conditions": [
                        {"left": {"ind": "DELTA", "field": "delta_pct"}, "op": ">", "right": {"value": 20}}]})
    assert engine.needs_minutes(s)   # DELTA is order flow: built from 1-minute data
    rv = _cross_spec(entry_long={"logic": "AND", "conditions": [{"left": {"ind": "RVOL"}, "op": ">", "right": {"value": 2}}]})
    assert not engine.needs_minutes(rv)   # RVOL only needs candle volume
    est = _estimate_bars(s, 0, 30 * 86400)
    assert est < MAX_BARS and est < 100_000


def test_optimizer_reports_deflated_sharpe_and_pbo():
    """Every grid search carries the two overfitting corrections; too short
    a sample says so instead of printing a number."""
    closes = [100 + (i % 20) for i in range(400)]
    spec = _cross_spec(exit_long=None, target={"type": "pct", "value": 1})
    res = engine.optimize(spec, {"X": bars(closes)}, [
        {"path": "entry_long.conditions.0.right.value", "values": [104, 108, 112]}], "net_pnl", min_trades=1)
    assert "note" in res["overfitting"]
    assert all("_daily" not in r for r in res["ranked"])


def test_overfitting_stats_use_the_full_trial_count():
    import random

    rng = random.Random(4)
    daily = [{d: rng.gauss(500 if j == 0 else 0, 2000) for d in range(60)} for j in range(12)]
    out = engine.overfitting_stats(daily, 1_000_000, n_trials=40)
    assert out["days"] == 60 and out["deflated_sharpe"]["n_trials"] == 40
    assert 0.0 <= out["deflated_sharpe"]["dsr"] <= 1.0
    assert 0.0 <= out["pbo"]["pbo"] <= 1.0 and out["pbo"]["n_configs"] == 12


def test_backtest_stops_when_funds_run_out_even_without_compounding():
    """Srinu 5/24 went to -Rs 2.4 lakh on Rs 1 lakh: with compounding off the
    losses never left the account, so it kept trading on money it didn't have."""
    closes = [100, 101, 106, 108, 80, 79] * 600       # each cycle: buy ~106, exit ~80
    spec = _cross_spec(sizing={"mode": "qty", "value": 100},
                       portfolio={"capital": 12000, "leverage": 1, "compounding": False})
    out = engine.run(spec, {"X": bars(closes, spread=0)})
    cash = 12000.0
    for t in out["trades"]:
        assert t["qty"] * t["entry"] <= cash          # never buys with money it doesn't have
        cash += t["net"]
    assert cash > 0 and out["stats"]["skipped"]["no_capital"] > 0


def test_atr_stop_on_an_option_premium_is_the_underlyings_atr_times_delta():
    """It used to be refused ("ATR on a premium makes no sense"), so the
    builder hid ATR for option strategies with levels on the option."""
    spec = _opt_spec(stop_loss={"type": "atr", "value": 1, "atr_period": 14})
    assert spec["instrument"]["levels_on"] == "instrument"
    leg = {"kind": "CE", "K": 24550.0, "exp": DAY0 + 3 * 86400}
    atm = engine.premium_atr({14: 40.0}, leg, 24550.0, DAY0, 0.14)[14]
    assert 15 < atm < 25                                   # ATM delta ~0.5 → about half the underlying's ATR
    deep = engine.premium_atr({14: 40.0}, {**leg, "K": 23000.0}, 24550.0, DAY0, 0.14)[14]
    assert deep > 38                                       # deep ITM moves almost 1:1
    priced = {**leg, "price": engine.fno.bs_price(24550.0, 24550.0, engine.fno.years_to(leg["exp"], DAY0), 0.14, "CE")}
    assert abs(engine.premium_atr({14: 40.0}, priced, 24550.0, DAY0)[14] - atm) < 0.5   # IV implied from the live price
    # backtests instead of a ValueError — once ATR(14) exists (20 candles before the
    # signal), with the stop set on the premium (never entered without it)
    closes = [24500] * 20 + [24520, 24540, 24560, 24600, 24650, 24700] + [24700] * 30
    tr = engine.run(spec, {"N": _nifty(closes)})["trades"]
    assert tr and tr[0]["stop"] is not None and tr[0]["stop"] < tr[0]["legs"][0]["entry"]


def test_stats_median_win_and_loss():
    t = lambda n: {"net": n, "gross": n, "charges": 0, "entry_t": 0, "exit_t": 60, "bars": 1}  # noqa: E731
    st = engine.stats([t(100), t(200), t(9000), t(-50), t(-70), t(-5000)], 100000)
    assert st["median_win"] == 200 and st["median_loss"] == -70       # one outlier doesn't move them
    assert st["avg_win"] == 3100


def test_daily_statistics_count_the_flat_sessions():
    # 4 trading days out of 40 sessions: the Sharpe is over all 40 days (flat = 0),
    # not the 4 with an exit — the latter would be ~3x higher for the same P&L.
    import math
    import statistics as stt

    k0 = (DAY0 + 19800) // 86400
    sessions = [k0 + k for k in range(40)]
    exits = {3: 100.0, 9: 50.0, 21: 120.0, 30: -40.0}
    tr = [{"net": v, "gross": v, "charges": 0, "entry_t": DAY0 + k * 86400, "exit_t": DAY0 + k * 86400 + 600, "bars": 2}
          for k, v in exits.items()]
    every = [exits.get(k, 0.0) for k in range(40)]
    st = engine.stats(tr, 100000, sessions)
    assert st["sharpe"] == round(stt.mean(every) / stt.stdev(every) * math.sqrt(252), 2)
    active = engine.stats(tr, 100000)                     # without the sessions: only the exit days
    assert active["sharpe"] == round(stt.mean(exits.values()) / stt.stdev(exits.values()) * math.sqrt(252), 2)
    assert active["sharpe"] > 2.5 * st["sharpe"]
    assert st["trading_days"] == 4 and st["profitable_days"] == 3 and st["worst_day"] == -40
    # CAGR over the 40-day range, not first entry to last exit (27 days)
    assert st["cagr_pct"] == pytest.approx(((100230 / 100000) ** (365 / 40) - 1) * 100, abs=0.01)
    # a backtest passes its sessions: a sparse strategy's Sharpe is over every day of the range
    d = _days(10)
    res = engine.run(_cross_spec(exit_long=None, target={"type": "pct", "value": 1},
                                 max_trades_per_day=1, session={"start": "09:15", "end": "15:00", "square_off": "15:15",
                                                               "days": [1, 3]}), {"X": d})
    per_day: dict[int, float] = {}
    for x in res["trades"]:
        per_day[(x["exit_t"] + 19800) // 86400] = per_day.get((x["exit_t"] + 19800) // 86400, 0.0) + x["net"]
    allv = [per_day.get(k, 0.0) for k in sorted({(t + 19800) // 86400 for t in d["t"]})]
    assert len(allv) == 10 and 1 < res["stats"]["trading_days"] < 10
    assert res["stats"]["sharpe"] == round(stt.mean(allv) / stt.stdev(allv) * math.sqrt(252), 2)
    # one trade in a month is no sample: no Sharpe / PSR / skew, as before
    lone = engine.stats(tr[:1], 100000, sessions)
    assert lone["sharpe"] is None and lone["psr_pct"] is None and lone["skew"] is None
    flat = engine.stats([], 100000, sessions)
    assert flat["sharpe"] is None and flat["var95"] is None and flat["cagr_pct"] is None


def test_flow_pressure_absorption_and_aggression():
    n = 30
    d = bars([100.0 + (i % 3) * 0.1 for i in range(n)], spread=1.0)
    d.update(flow=True, buy_v=[500.0 + (i % 4) * 20 for i in range(n)], sell_v=[500.0 + ((i + 2) % 4) * 20 for i in range(n)])
    # bar 25: 3000 sold at market, yet the bar closes at its high -> buyers absorbed it (a floor)
    d["sell_v"][25], d["h"][25], d["l"][25], d["c"][25] = 3500.0, 101.0, 99.0, 101.0
    # bar 27: 3000 bought at market and the close follows to the high -> initiative buying
    d["buy_v"][27], d["h"][27], d["l"][27], d["c"][27] = 3500.0, 102.0, 100.0, 102.0
    r = ind.compute(d, "FLOW_PRESSURE", {"period": 20, "smooth": 3, "threshold": 2.0})
    assert r["absorption"][25] == 1.0 and r["aggression"][25] == 0.0
    assert r["aggression"][27] == 1.0 and r["absorption"][27] == 0.0
    assert r["delta_z"][25] < -2 < 2 < r["delta_z"][27]
    assert sum(abs(x) for x in r["absorption"][:25]) == 0               # ordinary bars flag nothing
    assert r["pressure"][26] < 0 < r["pressure"][29]                     # the sell wave, then the buy wave
    assert "FLOW_PRESSURE" in engine.ORDER_FLOW


def test_stats_tail_risk_and_confidence():
    t = lambda k, v: {"net": v, "gross": v, "charges": 0, "entry_t": k * 86400, "exit_t": k * 86400 + 60, "bars": 1}  # noqa: E731
    pnl = [300, -100, 250, -120, 400, -90, 180, -2000, 220, 310, -80, 260, -150, 190, 330, -110, 240, 280, -95, 205]
    st = engine.stats([t(k, v) for k, v in enumerate(pnl)], 100000)
    assert st["var95"] == 150 and st["cvar95"] == 2000                   # 5th-percentile day; the one worst day
    assert st["skew"] < 0 and st["excess_kurtosis"] > 3                  # one crash day: left tail, fat tails
    assert 50 < st["psr_pct"] < 100 and st["sqn"] > 0
    few = engine.stats([t(k, v) for k, v in enumerate(pnl[:5])], 100000)
    assert few["var95"] is None and few["tail_ratio"] is None            # too few days to say


# ---- flexibility: scale-out, weekday filter, walk-forward, heatmap -----------

def test_old_specs_normalize_unchanged():
    """New options are absent from a normalized spec unless switched on, so a
    spec saved before them re-normalizes to exactly itself (no spurious version)."""
    old = _cross_spec()
    assert "scale_out" not in old and "days" not in old["session"]
    assert engine.normalize(old) == old
    assert _cross_spec(scale_out=[], session={**old["session"], "days": []}) == old
    with pytest.raises(ValueError, match="more than 100%"):
        _cross_spec(stop_loss={"type": "points", "value": 2},
                    scale_out=[{"type": "rr", "value": 1, "qty_pct": 60}, {"type": "rr", "value": 2, "qty_pct": 50}])
    with pytest.raises(ValueError, match="needs a stop loss"):
        _cross_spec(scale_out=[{"type": "rr", "value": 1, "qty_pct": 50}])
    with pytest.raises(ValueError, match="weekday"):
        _cross_spec(session={**old["session"], "days": [0, 9]})


def test_scale_out_books_partials_then_target_takes_the_rest():
    closes = [100, 101, 102, 106, 108.5, 110, 113, 113, 113]
    spec = _cross_spec(exit_long=None, sizing={"mode": "qty", "value": 10},
                       stop_loss={"type": "points", "value": 2}, target={"type": "rr", "value": 3},
                       scale_out=[{"type": "rr", "value": 1, "qty_pct": 50}])
    tr = engine.run(spec, {"X": bars(closes, spread=0)})["trades"]
    assert [(t["reason"], t["qty"]) for t in tr] == [("SCALE_OUT_1", 5), ("TARGET", 5)]
    # A market entry fills at the entry bar's OPEN (106), so the whole bar comes after
    # the fill: 1R = 108 is reached inside it. 3R = 112 is touched inside a later bar.
    assert tr[0]["exit"] == pytest.approx(108) and tr[1]["exit"] == pytest.approx(112)
    assert tr[0]["exit_t"] == tr[0]["entry_t"]
    assert [t["gross"] for t in tr] == [10.0, 30.0] and [t["r"] for t in tr] == [1.0, 3.0]
    # a step worth less than one set (half of one share / one lot) is skipped, not rounded up
    assert engine.scale_plan([(108.0, 50)], 1) == []
    assert engine.scale_plan([(108.0, 50), (110.0, 50)], 3) == [[108.0, 1], [110.0, 1]]
    # without the scale-out the same trade is one full exit
    plain = engine.run(_cross_spec(exit_long=None, sizing={"mode": "qty", "value": 10},
                                   stop_loss={"type": "points", "value": 2}, target={"type": "rr", "value": 3}),
                       {"X": bars(closes, spread=0)})["trades"]
    assert [(t["reason"], t["qty"]) for t in plain] == [("TARGET", 10)]


def test_weekday_filter_blocks_entries_on_other_days():
    closes = [100, 101, 102, 106, 108, 110, 104, 102, 101]          # DAY0 = Wed 2025-01-01
    sess = {"start": "09:15", "end": "15:00", "square_off": "15:15"}
    assert engine.run(_cross_spec(session={**sess, "days": [1, 2]}), {"X": bars(closes, spread=0)})["trades"] == []
    assert len(engine.run(_cross_spec(session={**sess, "days": [3]}), {"X": bars(closes, spread=0)})["trades"]) == 1
    assert engine.day_allowed(_cross_spec(session={**sess, "days": [3]}), DAY0)
    assert not engine.day_allowed(_cross_spec(session={**sess, "days": [4, 5]}), DAY0)


def _days(n_days: int):
    """n sessions of 75 x 5m bars, a saw-tooth that crosses 105 every day."""
    t, c = [], []
    for k in range(n_days):
        for i in range(75):
            t.append(DAY0 + k * 86400 + i * 300)
            c.append(100 + (i % 25) * 0.5)
    d = bars(c, spread=0.2)
    d["t"] = t
    return d


def test_walk_forward_ranks_in_sample_and_trades_unseen_windows():
    d = _days(24)
    spec = _cross_spec(exit_long=None, target={"type": "pct", "value": 1}, stop_loss={"type": "pct", "value": 1})
    grid = [{"path": "entry_long.conditions.0.right.value", "values": [104, 105, 106]}]
    wf = engine.walk_forward(spec, {"X": d}, grid, "net_pnl", min_trades=1, windows=3, is_mult=2)
    assert wf["combos"] == 3 and len(wf["folds"]) == 3
    for f in wf["folds"]:
        assert f["is_from"] < f["oos_from"] < f["oos_to"]
        assert f["params"]["entry_long.conditions.0.right.value"] in (104, 105, 106)
    assert wf["oos"]["trades"] == sum(f["oos"]["trades"] for f in wf["folds"]) > 0
    assert wf["oos"]["equity"][0][0] >= wf["folds"][0]["oos_from"]           # nothing from in-sample leaks in
    anch = engine.walk_forward(spec, {"X": d}, grid, min_trades=1, windows=3, is_mult=2, anchored=True)
    assert len({f["is_from"] for f in anch["folds"]}) == 1                     # in-sample always from the start
    with pytest.raises(ValueError, match="2-12"):
        engine.walk_forward(spec, {"X": d}, grid, windows=1)
    opt = engine.optimize(spec, {"X": d}, grid, min_trades=1)
    assert len(opt["all"]) == 3 and {tuple(r["params"].values())[0] for r in opt["all"]} == {104, 105, 106}


def test_runner_paper_scale_out_then_target(monkeypatch, isolated_db):
    """Paper runner: the scale-out closes half as its own row, the rest runs to
    the target, and the partial row doesn't count as another trade of the day."""
    import asyncio

    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _cross_spec(exit_long=None, sizing={"mode": "qty", "value": 10},
                       stop_loss={"type": "points", "value": 2}, target={"type": "points", "value": 6},
                       scale_out=[{"type": "rr", "value": 1, "qty_pct": 50}])
    with dbs.SessionLocal() as db:
        db.add(AlgoStrategy(name="so", spec=spec, enabled=True, mode="paper"))
        db.commit()
    ltp = {"px": 106.5}

    async def fake_bars(sym, tf, bars=400, now=None):
        d = globals()["bars"]([100, 101, 102, 106], spread=0)
        keep = [i for i, t in enumerate(d["t"]) if t + 300 <= now]
        return {k: ([v[i] for i in keep] if isinstance(v, list) else v) for k, v in d.items()}

    async def fake_ltp(symbols):
        return {s.upper(): ltp["px"] for s in symbols}

    monkeypatch.setattr(rn.data, "recent_bars", fake_bars)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    r = rn.AlgoRunner()
    t_close = DAY0 + 4 * 300 + 4
    asyncio.run(r.tick(t_close))
    with dbs.SessionLocal() as db:
        assert db.query(AlgoTrade).one().scale == {"left": 10, "sets": 10, "levels": [[108.5, 5]], "done": 0}
    ltp["px"] = 109.0
    asyncio.run(r.tick(t_close + 5))
    with dbs.SessionLocal() as db:
        rows = db.query(AlgoTrade).order_by(AlgoTrade.id).all()
        assert [(x.status, x.exit_reason, x.legs[0]["qty"]) for x in rows] == [
            ("open", None, 5), ("closed", "SCALE_OUT_1", 5)]
        assert rows[1].gross_pnl == pytest.approx(12.5) and rows[0].scale["done"] == 1
        sid = rows[0].strategy_id
    assert rn._trades_today(sid, "NSE:TEST-EQ", DAY0 - 33300) == 1
    ltp["px"] = 113.0
    asyncio.run(r.tick(t_close + 10))
    with dbs.SessionLocal() as db:
        rows = db.query(AlgoTrade).order_by(AlgoTrade.id).all()
        assert [(x.status, x.exit_reason) for x in rows] == [("closed", "TARGET"), ("closed", "SCALE_OUT_1")]
        assert rows[0].gross_pnl == pytest.approx(5 * 6.5)


def test_angle_transform_is_atr_scaled_degrees():
    d = bars([100.0 + i for i in range(300)], spread=0)         # +1 a bar, true range 1 -> ATR 1 (once warmed up)
    ang = engine.series(d, {"ind": "PRICE", "params": {}, "xf": "angle", "xn": 1}, {})
    assert abs(ang[-1] - 45.0) < 1e-3                           # one ATR per bar = 45 degrees
    flat = engine.series(bars([100.0] * 40, spread=1), {"ind": "PRICE", "params": {}, "xf": "angle", "xn": 3}, {})
    assert flat[-1] == 0.0
    ch = engine.series(d, {"ind": "PRICE", "params": {}, "xf": "pct_change", "xn": 2}, {})
    assert abs(ch[-1] - 2 / 397 * 100) < 1e-9


def test_between_held_within_negate_and_math_operands():
    d = bars([10, 11, 12, 13, 14, 15, 14, 13, 12, 11], spread=0)
    P = {"ind": "PRICE", "params": {}}
    ev = lambda c: engine._cond(d, c, {})  # noqa: E731
    assert ev({"left": P, "op": "between", "right": {"value": 15}, "right2": {"value": 12}}) == \
        [False, False, True, True, True, True, True, True, True, False]       # bounds in either order
    assert ev({"left": P, "op": "outside", "right": {"value": 12}, "right2": {"value": 14}}) == \
        [True, True, False, False, False, True, False, False, False, True]
    assert ev({"left": P, "op": ">", "right": {"value": 11}, "for_bars": 3})[:5] == [False, False, False, False, True]
    within = ev({"left": P, "op": "crosses_above", "right": {"value": 12.5}, "within_bars": 3})
    assert within[3:7] == [True, True, True, False]                          # the cross at bar 3, two bars on
    assert ev({"left": P, "op": ">", "right": {"value": 13}, "negate": True})[4] is False
    spread = {"math": "pct_diff", "a": P, "b": {"value": 10}}
    assert abs(engine.series(d, spread, {})[5] - 50.0) < 1e-9                # 15 is 50% above 10


def test_ema_cross_at_an_angle_between_30_and_45_degrees_builds_and_runs():
    E = lambda p: {"ind": "EMA", "params": {"period": p}}  # noqa: E731
    cross = {"left": E(9), "op": "crosses_above", "right": E(21)}

    def spec(lo, hi):
        conds = [cross] + ([{"left": {**E(9), "xf": "angle", "xn": 3}, "op": "between",
                             "right": {"value": lo}, "right2": {"value": hi}}] if lo is not None else [])
        return engine.normalize({
            "symbols": ["NSE:TEST-EQ"], "timeframe": 5, "direction": "long",
            "entry_long": {"logic": "AND", "conditions": conds},
            "exit_long": {"logic": "AND", "conditions": [{"left": E(9), "op": "crosses_below", "right": E(21)}]},
            "stop_loss": None, "target": None, "costs": {"slippage_pct": 0, "charges": False}})
    closes = [100 - 0.3 * i for i in range(40)] + [88 + 0.6 * i for i in range(30)] + [106 - 0.5 * i for i in range(30)]
    d = bars(closes, spread=0.2)
    sig = engine.evaluate(d, spec(None, None)["entry_long"], {})
    i = sig.index(True)                                          # the plain crossover
    a = engine.series(d, {**E(9), "xf": "angle", "xn": 3}, {})[i]
    assert engine.run(spec(None, None), {"X": d})["stats"]["trades"] == 1
    assert engine.run(spec(a - 5, a + 5), {"X": d})["stats"]["trades"] == 1    # crossed inside the band: taken
    assert engine.run(spec(a + 5, a + 15), {"X": d})["stats"]["trades"] == 0   # too shallow for it: skipped
    with pytest.raises(ValueError, match="low and a high"):
        engine.normalize({**spec(None, None), "entry_long": {"logic": "AND", "conditions": [
            {"left": E(9), "op": "between", "right": {"value": 30}}]}})
    with pytest.raises(ValueError, match="unknown transform"):
        engine.normalize({**spec(None, None), "entry_long": {"logic": "AND", "conditions": [
            {"left": {**E(9), "xf": "curvature"}, "op": ">", "right": {"value": 0}}]}})


# ---- option selling: SPAN-style margin, delta strikes, own-expiry legs, payoff ----

def _leg(kind, act, K, price, exp, qty=65):
    return {"kind": kind, "act": act, "K": K, "price": price, "exp": exp, "qty": qty}


def test_writing_an_option_blocks_span_plus_exposure_buying_only_the_premium():
    from app.algo import fno

    t, exp, s = DAY0, DAY0 + 6 * 86400, 25000.0
    px = lambda k, r: fno.bs_price(s, k, fno.years_to(exp, t), 0.13, r)  # noqa: E731
    m = lambda legs: fno.span_margin(legs, s, t, name="NIFTY", iv=0.13)  # noqa: E731
    bought = m([_leg("CE", 1, 25000, px(25000, "CE"), exp)])
    sold = m([_leg("CE", -1, 25000, px(25000, "CE"), exp)])
    assert bought["total"] == bought["premium"] == pytest.approx(px(25000, "CE") * 65, abs=0.01)
    assert sold["exposure"] == pytest.approx(0.02 * s * 65) and sold["total"] > 6 * bought["total"]
    strangle = [_leg("CE", -1, 25500, px(25500, "CE"), exp), _leg("PE", -1, 24500, px(24500, "PE"), exp)]
    condor = strangle + [_leg("CE", 1, 25800, px(25800, "CE"), exp), _leg("PE", 1, 24200, px(24200, "PE"), exp)]
    assert m(condor)["span"] < m(strangle)["span"] / 2                 # the wings cap the worst loss
    straddle = m([_leg("CE", -1, 25000, px(25000, "CE"), exp), _leg("PE", -1, 25000, px(25000, "PE"), exp)])
    assert straddle["span"] < sold["span"]                             # only one side can lose
    fut = m([{"kind": "FUT", "act": 1, "qty": 65, "price": s}])
    assert fut["total"] == pytest.approx(0.08 * s * 65)                # 6% scan + 2% exposure
    # expiry day: SEBI's extra 2% on short index options; IV implied from the quote when not given
    on_expiry = fno.span_margin([_leg("CE", -1, 25000, 120.0, DAY0 + 5 * 3600)], s, DAY0, name="NIFTY")
    assert on_expiry["exposure"] == pytest.approx(0.04 * s * 65)
    assert fno.implied_vol(px(25000, "CE"), s, 25000, fno.years_to(exp, t), "CE") == pytest.approx(0.13, abs=1e-4)


def test_span_margin_model_lets_a_short_straddle_trade_where_notional_cannot():
    closes = [24500, 24520, 24540, 24560] + [24560] * 8
    legs = [{"right": "CE", "action": "SELL", "lots": 1}, {"right": "PE", "action": "SELL", "lots": 1}]
    base = dict(instrument={"type": "option", "expiry_kind": "weekly", "iv": {"source": "fixed", "value": 14},
                            "legs_long": legs, "levels_on": "underlying"},
                portfolio={"capital": 300_000, "leverage": 1}, sizing={"mode": "lots", "value": 1})
    lev = engine.run(_opt_spec(**base), {"N": _nifty(closes)})
    assert lev["trades"] == [] and lev["stats"]["skipped"]["no_capital"] == 1   # 2 x notional ≈ ₹32 lakh
    spec = _opt_spec(**base, margin={"model": "span"})
    t = engine.run(spec, {"N": _nifty(closes)})["trades"][0]
    assert 100_000 < t["margin"] < 300_000 and len(t["legs"]) == 2
    assert "margin" not in _opt_spec(**base, margin={"model": "leverage"})     # leverage = the default: left out
    with pytest.raises(ValueError, match="margin model"):
        _opt_spec(**base, margin={"model": "var"})


def test_delta_strikes_and_a_leg_on_its_own_expiry():
    from app.algo import fno

    closes = [24500, 24520, 24540, 24560, 24600, 24650, 24700] + [24700] * 10
    spec = _opt_spec(instrument={"type": "option", "expiry_kind": "weekly", "levels_on": "underlying",
                                 "iv": {"source": "fixed", "value": 14}, "legs_long": [
                                     {"right": "CE", "action": "SELL", "strike": "DELTA", "delta": 0.2, "lots": 1},
                                     {"right": "CE", "action": "BUY", "strike": "ATM", "expiry": "next", "lots": 1}]},
                     margin={"model": "span"})
    t = engine.run(spec, {"N": _nifty(closes)})["trades"][0]
    short, long_ = t["legs"]
    k = float(short["label"].split()[2])
    dlt = fno.bs_greeks(24560, k, fno.years_to(fno.expiry_after("NIFTY", "NSE", "weekly", "current", DAY0), DAY0 + 1500),
                        0.14, "CE")["delta"]
    assert k > 24600 and abs(dlt - 0.2) < 0.06                          # an OTM call near 20 delta
    assert short["label"].split()[1] == "02JAN25" and long_["label"].split()[1] == "09JAN25"   # a calendar
    with pytest.raises(ValueError, match="target delta"):
        _opt_spec(instrument={"type": "option", "legs_long": [{"right": "CE", "action": "SELL", "strike": "DELTA",
                                                                "delta": 1.5, "lots": 1}]})
    with pytest.raises(ValueError, match="own expiry"):
        _opt_spec(instrument={"type": "option", "legs_long": [{"right": "CE", "action": "SELL", "expiry": "far",
                                                                "lots": 1}]})
    plain = _opt_spec()
    assert all("delta" not in lg and "expiry" not in lg for lg in plain["instrument"]["legs_long"])


def test_live_delta_strike_and_next_expiry_leg_from_the_chain(monkeypatch):
    import asyncio

    from app.algo import fno
    from app.algo import runner as rn

    now, e1, e2 = 1790000000, 1790000000 + 5 * 86400, 1790000000 + 12 * 86400

    def chain(exp):
        yrs = fno.years_to(exp, now)
        return {"spot": 24537, "expiries": [{"ts": str(e1)}, {"ts": str(e2)}],
                "strikes": [{"strike": k, "ce": {"symbol": f"NSE:N{exp}X{k}CE", "ltp": fno.bs_price(24537, k, yrs, 0.14, "CE")}}
                            for k in range(24000, 25100, 50)]}

    asked = []

    class B:
        async def get_option_chain(self, sym, strikecount=10, timestamp=""):
            asked.append(timestamp)
            return chain(int(timestamp) if timestamp else e1)

    monkeypatch.setattr(rn.data, "_backend", lambda: B())
    spec = _opt_spec(instrument={"type": "option", "levels_on": "underlying", "legs_long": [
        {"right": "CE", "action": "SELL", "strike": "DELTA", "delta": 0.25, "lots": 1},
        {"right": "CE", "action": "BUY", "strike": "ATM", "expiry": "next", "lots": 1}]})
    legs = asyncio.run(rn.build_legs(spec, "NSE:NIFTY50-INDEX", "BUY", 24537, now))
    want = fno.strike_for_delta(24537, 50, "CE", 0.25,
                                lambda k: fno.bs_greeks(24537, k, fno.years_to(e1, now), 0.14, "CE")["delta"])
    assert legs[0]["K"] == want and legs[0]["exp"] == e1 and legs[1]["exp"] == e2 and legs[1]["K"] == 24550
    assert asked == ["", str(e2)]                                       # each chain fetched once


def test_payoff_preview_of_a_bull_call_spread():
    from app.algo import payoff

    dummy = {"logic": "AND", "conditions": [{"left": {"value": 1}, "op": ">", "right": {"value": 0}}]}
    spec = engine.normalize({
        "symbols": ["NSE:NIFTY50-INDEX"], "direction": "long", "entry_long": dummy, "stop_loss": None, "target": None,
        "margin": {"model": "span"},
        "instrument": {"type": "option", "levels_on": "underlying", "legs_long": [
            {"right": "CE", "action": "BUY", "strike": "ATM", "lots": 1},
            {"right": "CE", "action": "SELL", "strike": "OTM", "steps": 4, "lots": 1}]}})
    meta = {"symbol": "NSE:NIFTY50-INDEX", "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
    r = payoff.preview(spec, "BUY", 25000, DAY0, 0.13, meta)
    debit = -r["premium"]["net"]
    assert r["premium"]["kind"] == "debit" and len(r["legs"]) == 2
    assert r["max_loss"] == pytest.approx(-debit, rel=0.02)              # lose the debit below 25000
    assert r["max_profit"] == pytest.approx(200 * 65 - debit, rel=0.02)  # the 200-point width minus it
    assert len(r["breakevens"]) == 1 and 25000 < r["breakevens"][0] < 25200
    assert 0 < r["pop_pct"] < 100 and r["greeks"]["delta"] > 0 and r["greeks"]["theta"] < 0
    m = r["margin"]
    assert m["model"] == "span" and m["used"] == m["span"]["total"] < m["naked"] and m["hedge_benefit"] > 0
    assert r["legs"][1]["margin_if_sold"] > 5 * r["legs"][1]["margin_if_bought"]   # writing costs far more
    assert r["curve"][0]["expiry"] == pytest.approx(-debit, rel=0.02)


def test_payoff_endpoint(client, monkeypatch):
    from app.algo import fno

    async def no_download(max_age_h=20.0):
        return None

    monkeypatch.setattr(fno, "ensure_master", no_download)
    spec = {"symbols": ["NSE:NIFTY50-INDEX"], "instrument": {
        "type": "option", "levels_on": "underlying", "iv": {"source": "fixed", "value": 12},
        "legs_long": [{"right": "CE", "action": "SELL", "lots": 1}, {"right": "PE", "action": "SELL", "lots": 1}],
        "legs_short": [{"right": "PE", "action": "BUY", "lots": 1}]}}
    r = client.post("/api/algo/payoff", json={"spec": spec, "spot": 25000}).json()
    assert r["iv_pct"] == 12 and r["iv_source"] == "fixed" and r["premium"]["kind"] == "credit"
    assert r["max_loss"] is None and len(r["breakevens"]) == 2         # a naked straddle: unlimited risk
    assert r["margin"]["span"]["total"] < r["margin"]["leverage"]
    assert client.post("/api/algo/payoff", json={"spec": spec, "spot": 25000, "side": "SELL"}).json()["legs"][0]["side"] == "BUY"
    assert client.post("/api/algo/payoff", json={"spec": {**spec, "symbols": []}}).status_code == 422
    ruled = {**spec, "position_rules": {"entry": [{"metric": "credit", "op": ">=", "value": 10**7}]}}
    r = client.post("/api/algo/payoff", json={"spec": ruled, "spot": 25000}).json()
    assert r["entry_rules"] == [{"rule": "credit >= 10000000", "metric": "credit", "value": r["position"]["credit"], "ok": False}]


# ---- option Greeks: full Greeks, leg stops, MTM % of premium, position rules, VIX, rank ----

def test_every_greek_matches_a_numerical_derivative():
    from app.algo import fno

    S, K, T, v, r0 = 25000.0, 25200.0, 7 / 365, 0.13, fno.RISK_FREE
    dt, dv, h = 1 / 365 / 240, 1e-4, 1.0
    for right in ("CE", "PE"):
        g = fno.bs_greeks(S, K, T, v, right)
        p = lambda s=S, t=T, vv=v, r=r0: fno.bs_price(s, K, t, vv, right, r)  # noqa: E731
        G = lambda s=S, t=T, vv=v: fno.bs_greeks(s, K, t, vv, right)  # noqa: E731
        num = {"delta": (p(S + h) - p(S - h)) / 2 / h,
               "gamma": (p(S + h) - 2 * p() + p(S - h)) / h / h,
               "theta": (p(t=T - dt) - p(t=T + dt)) / 2 / dt / 365,
               "vega": (p(vv=v + dv) - p(vv=v - dv)) / 2 / dv / 100,
               "rho": (p(r=r0 + 1e-5) - p(r=r0 - 1e-5)) / 2e-5 / 100,
               "vanna": (G(vv=v + dv)["delta"] - G(vv=v - dv)["delta"]) / 2 / dv / 100,
               "charm": (G(t=T - dt)["delta"] - G(t=T + dt)["delta"]) / 2 / dt / 365,
               "vomma": (G(vv=v + dv)["vega"] - G(vv=v - dv)["vega"]) / 2 / dv / 100,
               "speed": (G(s=S + h)["gamma"] - G(s=S - h)["gamma"]) / 2 / h}
        for k, x in num.items():
            assert g[k] == pytest.approx(x, rel=2e-3, abs=1e-9), (right, k)
        assert 0 < g["prob_itm"] < 1
    assert fno.bs_greeks(S, K, 0, v, "PE") == {"delta": -1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0,
                                                "vanna": 0.0, "charm": 0.0, "vomma": 0.0, "speed": 0.0, "prob_itm": 1.0}


def test_rank_and_percentile_transforms():
    d = bars([1.0, 2, 3, 4, 5, 3, 1])
    P = {"ind": "PRICE", "params": {}}
    assert engine.series(d, {**P, "xf": "rank", "xn": 3}, {}) == [None, None, 100.0, 100.0, 100.0, 0.0, 0.0]
    assert engine.series(d, {**P, "xf": "percentile", "xn": 3}, {}) == [None, None, None, 100.0, 100.0, 0.0, 0.0]
    assert engine.series(bars([2.0, 2, 2]), {**P, "xf": "rank", "xn": 2}, {})[-1] == 50.0      # flat: mid-range


_RISE_FALL = [24500, 24520, 24540, 24560] + [24560 + 40 * k for k in range(1, 10)] + [24900 - 60 * k for k in range(1, 10)]


def _strangle(on=None, **leg_kw):
    legs = [{"right": "CE", "action": "SELL", "strike": "OTM", "steps": 2, "lots": 1, **leg_kw},
            {"right": "PE", "action": "SELL", "strike": "OTM", "steps": 2, "lots": 1, **leg_kw}]
    return {"type": "option", "expiry_kind": "weekly", "levels_on": "underlying", "legs_long": legs,
            "iv": {"source": "fixed", "value": 14}, **({"on_leg_stop": on} if on else {})}


def test_leg_stops_close_one_leg_and_can_move_the_rest_to_cost_or_exit_all():
    out = {}
    for on in ("leg", "cost", "all"):
        t = engine.run(_opt_spec(instrument=_strangle(on, sl_pct=30)), {"N": _nifty(_RISE_FALL)})["trades"]
        assert len(t) == 1                                              # one position, every leg in it
        out[on] = t[0]
    ce, pe = out["leg"]["legs"]
    assert ce["reason"] == "LEG_SL" and ce["exit"] == pytest.approx(ce["entry"] * 1.3, abs=0.02)   # +30% of premium
    assert pe["reason"] == "LEG_SL" and pe["exit"] == pytest.approx(pe["entry"] * 1.3, abs=0.02)   # later, on the fall
    ce, pe = out["cost"]["legs"]
    assert ce["reason"] == "LEG_SL" and pe["reason"] == "LEG_COST" and pe["exit"] == pytest.approx(pe["entry"], abs=0.02)
    assert out["cost"]["reason"] == "LEG_COST" and out["cost"]["net"] > out["leg"]["net"]          # cost stop saved money
    ce, pe = out["all"]["legs"]
    assert out["all"]["reason"] == "LEG_SL" and "reason" not in pe and pe["exit"] < pe["entry"]     # the put still in profit
    g = out["leg"]["greeks"]
    assert g["credit"] > 0 and g["theta"] > 0 and g["vega"] < 0 and abs(g["delta"]) < 0.2 and g["iv"] == 14.0
    with pytest.raises(ValueError, match="option legs"):
        _opt_spec(instrument={"type": "option", "levels_on": "underlying", "legs_long": [
            {"right": "FUT", "action": "BUY", "lots": 1, "sl_pct": 10}]})
    with pytest.raises(ValueError, match="can't be combined"):
        _opt_spec(instrument=_strangle(sl_pct=30), stop_loss={"type": "points", "value": 50},
                  scale_out=[{"type": "rr", "value": 1, "qty_pct": 50}])
    with pytest.raises(ValueError, match="on a leg's stop"):
        _opt_spec(instrument=_strangle("hedge", sl_pct=30))


def test_leg_target_closes_only_that_leg():
    rise = _RISE_FALL[:13]
    t = engine.run(_opt_spec(instrument=_strangle(tg_pct=60)), {"N": _nifty(rise)})["trades"][0]
    ce, pe = t["legs"]
    assert "reason" not in ce and pe["reason"] == "LEG_TARGET" and pe["exit"] == pytest.approx(pe["entry"] * 0.4, abs=0.02)
    assert t["reason"] == "END"                                        # the call ran on to the end of the data
    both = engine.run(_opt_spec(instrument=_strangle(tg_pct=60)), {"N": _nifty(_RISE_FALL)})["trades"][0]
    assert [lg["reason"] for lg in both["legs"]] == ["LEG_TARGET", "LEG_TARGET"] and both["reason"] == "LEG_TARGET"


def test_mtm_stop_and_target_as_a_percent_of_the_credit():
    spec = _opt_spec(instrument=_strangle(), mtm={"stop_pct": 50}, sizing={"mode": "lots", "value": 1})
    t = engine.run(spec, {"N": _nifty(_RISE_FALL)})["trades"][0]
    credit = sum(lg["entry"] * lg["qty"] for lg in t["legs"])
    assert t["reason"] == "MTM_SL" and t["gross"] == pytest.approx(-0.5 * credit, rel=0.01)   # lost half the credit
    flat = [24500, 24520, 24540, 24560] + [24560] * 40                  # time decay only
    spec = _opt_spec(instrument=_strangle(), mtm={"target_pct": 5}, sizing={"mode": "lots", "value": 1})
    t = engine.run(spec, {"N": _nifty(flat)})["trades"][0]
    credit = sum(lg["entry"] * lg["qty"] for lg in t["legs"])
    assert t["reason"] == "MTM_TARGET" and t["gross"] == pytest.approx(0.05 * credit, rel=0.01)
    assert engine.mtm_limits({"stop": 500, "target": None, "stop_pct": 100}, [
        {"kind": "CE", "act": -1, "entry": 10.0, "qty": 65}]) == (500, None)        # the tighter of ₹ and %
    with pytest.raises(ValueError, match="needs option legs"):
        _cross_spec(mtm={"stop_pct": 50})
    assert "stop_pct" not in _opt_spec(instrument=_strangle())["mtm"]              # off = left out


def test_position_rules_filter_entries_and_exit_on_delta():
    data = lambda: {"N": _nifty(_RISE_FALL)}  # noqa: E731
    rich = _opt_spec(instrument=_strangle(), position_rules={"entry": [{"metric": "credit", "op": ">=", "value": 10**6}]})
    r = engine.run(rich, data())
    assert r["trades"] == [] and r["stats"]["skipped"]["entry_rules"] == 1
    lean = _opt_spec(instrument=_strangle(), position_rules={"entry": [{"metric": "abs_delta", "op": "<=", "value": 0.2}],
                                                             "exit": [{"metric": "abs_delta", "op": ">", "value": 0.25}]})
    t = engine.run(lean, data())["trades"][0]
    assert t["reason"] == "RULE_ABS_DELTA"
    with pytest.raises(ValueError, match="metric is one of"):
        _opt_spec(instrument=_strangle(), position_rules={"exit": [{"metric": "beta", "op": ">", "value": 1}]})
    with pytest.raises(ValueError, match="need an option instrument"):
        _cross_spec(position_rules={"exit": [{"metric": "delta", "op": ">", "value": 1}]})
    assert "position_rules" not in _opt_spec(instrument=_strangle(), position_rules={"entry": [], "exit": []})


def test_position_metrics_per_set():
    from app.algo import fno

    t, exp, u = DAY0, DAY0 + 6 * 86400, 25000.0
    legs = [{"kind": "CE", "act": -1, "K": 25000, "exp": exp, "qty": 130, "per_set": 65, "entry": 200.0},
            {"kind": "PE", "act": -1, "K": 25000, "exp": exp, "qty": 130, "per_set": 65, "entry": 180.0}]
    px = {"CE": 150.0, "PE": 170.0}
    m = engine.position_metrics(legs, u, t, 65, lambda lg: px[lg["kind"]], None)
    assert m["credit"] == pytest.approx((150 + 170) * 65)                # per set, at today's prices
    assert m["pnl_pct"] == pytest.approx(((200 - 150) + (180 - 170)) / (200 + 180) * 100)
    ivs = [fno.implied_vol(px[k], u, 25000, fno.years_to(exp, t), k) for k in ("CE", "PE")]
    assert m["iv"] == pytest.approx(sum(ivs) / 2 * 100) and m["dte"] == pytest.approx(6.0)
    assert m["theta"] > 0 and m["vega"] < 0 and m["gamma"] < 0
    closed = [{**legs[0], "exit": 120.0}, legs[1]]
    m2 = engine.position_metrics(closed, u, t, 65, lambda lg: px[lg["kind"]], None)
    assert m2["credit"] == pytest.approx(170 * 65) and m2["pnl_pct"] == pytest.approx((80 + 10) / 380 * 100)
    assert engine.position_metrics(legs, u, t, 65, lambda lg: None, None)["delta"] is None   # no price: no guess


def test_vix_indicator_sees_only_closed_vix_candles():
    d = bars([100.0] * 6, tf=300)
    d["vix"] = bars([14.0, 15, 16, 17, 18, 19], tf=300)
    d["htf"] = {15: bars([100.0, 100], tf=900)}
    d["htf"][15]["vix"] = bars([20.0, 21], tf=900)
    d["tf_min"] = 5
    assert engine.series(d, {"ind": "VIX", "params": {}}, {}) == [14.0, 15, 16, 17, 18, 19]
    assert engine.series(d, {"ind": "VIX", "params": {}, "tf": 15}, {}) == [None, None, 20.0, 20.0, 20.0, 21.0]
    assert engine.series(bars([1.0, 2]), {"ind": "VIX", "params": {}}, {}) == [None, None]      # not loaded: never true
    spec = _cross_spec(entry_long={"logic": "AND", "conditions": [
        {"left": {"ind": "VIX", "tf": 1440, "xf": "rank", "xn": 200}, "op": ">", "right": {"value": 50}}]})
    assert engine.vix_timeframes(spec) == {1440} and engine.vix_timeframes(_cross_spec()) == set()


# ---- the Algo guide: every example imports, the reference covers everything ----

def test_every_json_example_in_the_guide_is_a_valid_strategy():
    import json
    import re

    from app.algo import guide

    text = guide.text()
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.S)
    assert len(blocks) >= 10
    for b in blocks:
        obj = json.loads(b)                                   # plain ```json blocks must be real JSON
        spec = engine.normalize(obj["spec"])                  # …and a strategy the importer accepts
        assert obj["name"] and spec["symbols"]
    for c in ind.catalog():
        assert f"| `{c['name']}` |" in text and c["about"], c["name"]
    for word in (*engine.OPS, *engine.TRANSFORMS, *engine.POSITION_METRICS, *engine.STRIKE_MODES):
        assert f"`{word}`" in text, word


def test_guide_endpoint(client):
    r = client.get("/api/algo/guide")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/markdown")
    assert r.text.startswith("# Algo Lab guide") and "| `VIX` |" in r.text


def _sessions(days: int, start_px: float, seed: int = 3) -> list[tuple[int, float, float, float, float, int]]:
    """1-minute candles 09:15-15:29 IST on `days` weekdays from 2025-01-01: a seeded random walk."""
    import random

    rnd = random.Random(seed)
    rows, px, day, done = [], start_px, 0, 0
    t0 = 1735689600 - 19800                                   # 2025-01-01 00:00 IST
    while done < days:
        d0 = t0 + day * 86400
        day += 1
        if ((d0 + 19800) // 86400 + 3) % 7 >= 5:              # weekend
            continue
        done += 1
        for m in range(375):
            o = px
            px = max(1.0, px * (1 + rnd.gauss(0, 0.0007)))
            hi, lo = max(o, px) * (1 + abs(rnd.gauss(0, 0.0002))), min(o, px) * (1 - abs(rnd.gauss(0, 0.0002)))
            rows.append((d0 + 33300 + m * 60, o, hi, lo, px, 1000 + rnd.randrange(500)))
    return rows


def _agg(rows: list, tf_min: int) -> dict:
    """Candles of `tf_min` from 1-minute rows, bucketed the way data.load does (1440 = daily at IST midnight)."""
    from app.algo import data as data_mod

    out: dict[int, list] = {}
    for t, o, h, lo, c, v in rows:
        day0 = t - (t + 19800) % 86400
        b = day0 if tf_min == 1440 else day0 + 33300 + ((t - day0 - 33300) // (tf_min * 60)) * tf_min * 60
        r = out.get(b)
        out[b] = [b, o, h, lo, c, v] if r is None else [b, r[1], max(r[2], h), min(r[3], lo), c, r[5] + v]
    return data_mod.bars_from([out[k] for k in sorted(out)], tf_min * 60)


def test_every_guide_example_backtests_on_synthetic_candles():
    import json
    import re

    from app.algo import fno, guide

    vix = _sessions(40, 14.0, seed=11)
    for b in re.findall(r"```json\n(.*?)\n```", guide.text(), re.S):
        spec = engine.normalize(json.loads(b)["spec"])
        data, xs = {}, {}
        for j, (ref, tfs) in enumerate(engine.ref_frames(spec).items()):   # other symbols the conditions read
            m1 = _sessions(40, 24000.0, seed=100 + j)
            xs[ref] = _agg(m1, spec["timeframe"]) | {"tf_min": spec["timeframe"],
                                                     "htf": {tf: _agg(m1, tf) for tf in tfs - {spec["timeframe"]}}}
        for k, sym in enumerate(spec["symbols"]):
            name = fno.fno_name(sym)
            m1 = _sessions(40, 24000.0 if name in fno.INDEX_LOTS else 1500.0, seed=k)
            d = _agg(m1, spec["timeframe"])
            d["tf_min"] = spec["timeframe"]
            d["htf"] = {tf: _agg(m1, tf) for tf in engine.cond_timeframes(spec)}
            for tf in engine.vix_timeframes(spec):
                if tf == spec["timeframe"]:
                    d["vix"] = _agg(vix, tf)
                else:
                    d["htf"][tf]["vix"] = _agg(vix, tf)
            d["meta"] = {"symbol": sym, "name": name, "exch": fno.exchange_of(sym), "lot": fno.lot_size(name),
                         "step": fno.default_step(name, d["c"][-1])}
            if xs:
                d["xs"] = xs
            data[sym] = d
        res = engine.run(spec, data, trade_from=data[spec["symbols"][0]]["t"][0] + 5 * 86400)
        assert res["stats"]["trades"] > 0, json.loads(b)["name"]          # an example that can never fire teaches nothing


def test_prices_sit_on_the_005_grid_and_slippage_rounds_against_you():
    assert engine.snap(100.03, 0.05) == 100.05 and engine.snap(100.02, 0.05) == 100.0
    assert engine.snap(100.01, 0.05, 1) == 100.05 and engine.snap(100.04, 0.05, -1) == 100.0
    assert engine.snap(808.15, 0.05, 1) == 808.15                      # already on the grid: unchanged
    assert engine.snap(0.01, 0.05, -1) == 0.05                         # never below one tick
    assert engine.tick_for("NSE:SBIN-EQ") == 0.05 and engine.tick_for("BSE:SENSEX-INDEX") == 0.05
    assert engine.tick_for("MCX:CRUDEOIL26NOVFUT") is None             # its own tick: left alone

    closes = [100.0, 101.0, 106.13, 107.21, 109.37, 103.11, 99.0]
    spec = _cross_spec(exit_long=None, stop_loss={"type": "pct", "value": 1.3}, target={"type": "pct", "value": 2.7},
                       costs={"slippage_pct": 0.07, "charges": False})
    t = engine.run(spec, {"NSE:TEST-EQ": bars(closes, spread=0)})["trades"][0]

    def on_grid(x):
        return abs(x / 0.05 - round(x / 0.05)) < 1e-6

    assert all(on_grid(x) for x in (t["entry"], t["exit"], t["stop"], t["target"])), t
    assert t["entry"] == 106.25                                        # 106.13 + 0.07% = 106.204 -> up to 106.25
    off = engine.run(spec, {"MCX:TEST": bars(closes, spread=0)})["trades"][0]
    assert off["entry"] == round(106.13 * 1.0007, 2)                   # not NSE / BSE: no grid
