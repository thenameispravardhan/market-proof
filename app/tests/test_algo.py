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
    assert t["reason"] == "SQUARE_OFF" and (t["exit_t"] + 19800) % 86400 >= 15 * 3600 + 29 * 60   # not 15:15


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
