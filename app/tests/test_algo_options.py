"""Algo Lab v3, half B: leg re-entry, rolls, delta hedges, IV skew, fills vs quotes (TCA)."""
from __future__ import annotations

import asyncio

import pytest

from app.algo import engine, fno
from app.tests.test_algo import DAY0, _RISE_FALL, _nifty, _opt_spec, _strangle, bars

NIFTY = "NSE:NIFTY50-INDEX"


# ---- B5: IV skew in option backtests --------------------------------------------

def test_skew_prices_otm_wings_richer_and_is_off_by_default():
    assert "put_skew" not in _opt_spec()["instrument"]["iv"]
    flat = _opt_spec(instrument=_strangle())
    sk = _opt_spec(instrument={**_strangle(), "iv": {"source": "fixed", "value": 14, "put_skew": 0.8, "call_skew": 0.3}})
    meta = {"symbol": NIFTY, "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
    u, t = 24500.0, DAY0
    a, b = engine.sim_legs(flat, "BUY", meta, u, t, 0.14), engine.sim_legs(sk, "BUY", meta, u, t, 0.14)
    ce, pe = b
    assert engine.leg_iv(pe, u, 0.14) == pytest.approx(0.14 + (u - pe["K"]) / u * 100 * 0.8 / 100)
    assert engine.leg_iv(ce, u, 0.14) == pytest.approx(0.14 + (ce["K"] - u) / u * 100 * 0.3 / 100)
    assert engine.leg_iv({**pe, "K": u + 100}, u, 0.14) == 0.14            # in the money: no skew
    for x, y in zip(a, b):                                                # same strikes, richer wings
        assert x["K"] == y["K"] and engine._leg_px(y, u, t, 0.14) > engine._leg_px(x, u, t, 0.14)
    for bad in ({"put_skew": -1}, {"call_skew": 11}):
        with pytest.raises(ValueError, match="skew"):
            _opt_spec(instrument={**_strangle(), "iv": {"source": "fixed", "value": 14, **bad}})


# ---- B1-B3: what a position does on a candle's close ----------------------------

def test_adjustment_specs_are_validated():
    s = _opt_spec(instrument=_strangle(sl_pct=30, reentries=2, roll={"above": 0.45, "to": 0.25}),
                  position_rules={"hedge": {"above": 0.6}})
    lg = s["instrument"]["legs_long"][0]
    assert lg["reentries"] == 2 and lg["roll"] == {"above": 0.45, "to": 0.25, "max": 1}
    assert s["position_rules"] == {"hedge": {"above": 0.6, "max": 10}} and engine.adjusts(s)
    assert not engine.adjusts(_opt_spec(instrument=_strangle(sl_pct=30)))
    for kw, msg in [(dict(instrument=_strangle("all", sl_pct=30, reentries=1)), "re-entries can't"),
                    (dict(instrument=_strangle(roll={"above": 0.2, "to": 0.3})), "0 < to < above"),
                    (dict(instrument={"type": "option", "legs_long": [{"right": "FUT", "action": "BUY", "lots": 1, "reentries": 1}]}), "option legs"),
                    (dict(position_rules={"hedge": {"above": 0.5}}, instrument={"type": "equity"}), "option instrument"),
                    (dict(instrument=_strangle(sl_pct=30, reentries=1), stop_loss={"type": "points", "value": 50},
                          scale_out=[{"type": "rr", "value": 1, "qty_pct": 50}]), "can't be combined")]:
        with pytest.raises(ValueError, match=msg):
            _opt_spec(**kw)


def _legs(u=24500.0, t=DAY0):
    meta = {"symbol": NIFTY, "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
    spec = _opt_spec(instrument={**_strangle(sl_pct=30, reentries=1, roll={"above": 0.4, "to": 0.2}),
                                 "legs_long": [{"right": "CE", "action": "SELL", "strike": "ATM", "lots": 2, "sl_pct": 30,
                                                "reentries": 1, "roll": {"above": 0.4, "to": 0.2}},
                                               {"right": "PE", "action": "SELL", "strike": "OTM", "steps": 4, "lots": 1}]},
                     position_rules={"hedge": {"above": 0.3, "max": 2}})
    legs = engine.sim_legs(spec, "BUY", meta, u, t, 0.14)
    for lg in legs:
        lg["entry"] = engine._leg_px(lg, u, t, 0.14)
        lg["qty"] = lg["qty0"] = lg["per_set"]
    return spec, legs


def test_adjustments_reenter_a_stopped_leg_roll_a_tested_one_and_hedge_the_delta():
    spec, legs = _legs()
    px = lambda lg: engine._leg_px(lg, 24500.0, DAY0, 0.14)  # noqa: E731
    acts = engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, px, 0.14)
    kinds = [a[0] for a in acts]
    assert "roll" in kinds                                  # the ATM short call has |delta| ~0.5 > 0.4
    roll = next(a for a in acts if a[0] == "roll")
    assert roll[1] == 0 and roll[2]["strike"] == "DELTA" and roll[2]["delta"] == 0.2
    hedge = next(a for a in acts if a[0] == "hedge")       # short ATM call + far put: net short delta
    assert hedge[1] == 1 and hedge[2] == {"right": "FUT", "action": "BUY", "lots": 1}
    legs[0].update(exit=legs[0]["entry"] * 1.3, exit_reason="LEG_SL")
    acts = engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, px, 0.14)
    assert ("reenter", 0) in [a[:2] for a in acts]
    assert [a[0] for a in engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, px, 0.14, opens=False)] \
        in ([], ["hedge"])                                  # past the entry window: hedges only
    legs.append({**legs[0], "exit": None, "exit_reason": None, "via": "REENTRY"})
    legs[0]["redone"] = True
    assert "reenter" not in [a[0] for a in engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, px, 0.14)]


def test_backtest_reenters_rolls_and_hedges():
    ran = engine.run(_opt_spec(instrument=_strangle(sl_pct=30, reentries=1)), {"N": _nifty(_RISE_FALL)})["trades"][0]
    assert [lg.get("via") for lg in ran["legs"]].count("REENTRY") >= 1      # the call stopped on the rise, sold again
    first, again = ran["legs"][0], next(lg for lg in ran["legs"] if lg.get("via") == "REENTRY")
    assert first["reason"] == "LEG_SL" and again["label"] != first["label"]     # a new strike near the new spot
    rolled = engine.run(_opt_spec(instrument=_strangle(roll={"above": 0.4, "to": 0.2})), {"N": _nifty(_RISE_FALL)})["trades"][0]
    assert any(lg.get("reason") == "LEG_ROLL" for lg in rolled["legs"]) and any(lg.get("via") == "ROLL" for lg in rolled["legs"])
    hedged = engine.run(_opt_spec(instrument=_strangle(), position_rules={"hedge": {"above": 0.5, "max": 3}}),
                        {"N": _nifty(_RISE_FALL)})["trades"][0]
    hedges = [lg for lg in hedged["legs"] if lg.get("via") == "HEDGE"]
    assert hedges and hedges[0]["side"] == "BUY" and "FUT" in hedges[0]["label"]   # short calls on a rally: buy the future
    plain = engine.run(_opt_spec(instrument=_strangle()), {"N": _nifty(_RISE_FALL)})["trades"][0]
    assert len(plain["legs"]) == 2                                                 # absent = exactly what it was


def test_runner_paper_reentry_opens_one_new_leg_at_the_quote(monkeypatch, isolated_db):
    from app.algo import runner as rn
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade

    spec = _opt_spec(instrument=_strangle(sl_pct=30, reentries=1))
    exp = fno.expiry_after("NIFTY", "NSE", "weekly", "current", DAY0)
    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="re", spec=spec, enabled=True, mode="paper")
        db.add(s)
        db.flush()
        tr = AlgoTrade(strategy_id=s.id, symbol=NIFTY, side="BUY", quantity=65, mode="paper", status="open",
                       entry_at=rn._utc(DAY0).replace(tzinfo=None), entry_price=24500.0, ref="u", ref_sign=1,
                       legs=[{"symbol": "NSE:OLDCE", "kind": "CE", "act": -1, "qty": 65, "qty0": 65, "per_set": 65, "entry": 100.0,
                              "K": 24600, "exp": exp, "src": 0, "exit": 130.0, "exit_reason": "LEG_SL", "label": "CE"},
                             {"symbol": "NSE:OLDPE", "kind": "PE", "act": -1, "qty": 65, "qty0": 65, "per_set": 65, "entry": 90.0,
                              "K": 24400, "exp": exp, "src": 1, "label": "PE"}])
        db.add(tr)
        db.commit()
        sid, tid = s.id, tr.id
    orders = []

    async def fake_legs(spec_, sym, side, u, now):
        cfg = spec_["instrument"]["legs_long"]
        assert len(cfg) == 1 and cfg[0]["right"] == "CE"            # one leg: the configured call again
        return [{"symbol": "NSE:NEWCE", "kind": "CE", "act": -1, "per_set": 65, "label": "NEWCE", "price": 120.0,
                 "K": 24800, "exp": exp, "src": 0, "sl_pct": 30}]

    async def fake_ltp(symbols):
        return {s.upper(): {"NSE:OLDPE": 60.0, NIFTY: 24800.0}.get(s.upper(), 120.0) for s in symbols}

    async def no_orders(*a, **k):
        orders.append(a)
        return False, None, "paper must not order"

    monkeypatch.setattr(rn, "build_legs", fake_legs)
    monkeypatch.setattr(rn, "_ltp", fake_ltp)
    monkeypatch.setattr(rn, "_order", no_orders)
    r = rn.AlgoRunner()
    s_ = {"id": sid, "name": "re", "spec": spec}
    now = DAY0 + 3600
    asyncio.run(r._adjust(s_, rn._fresh(tid), NIFTY, now))
    asyncio.run(r._adjust(s_, rn._fresh(tid), NIFTY, now + 300))         # used up: nothing more
    with dbs.SessionLocal() as db:
        legs = db.get(AlgoTrade, tid).legs
    assert not orders and len(legs) == 3 and legs[0]["redone"]
    new = legs[2]
    assert (new["via"], new["src"], new["entry"], new["entry_quote"], new["qty"]) == ("REENTRY", 0, 120.0, 120.0, 65)
    assert new["sl"] == pytest.approx(120.0 * 1.3)                      # its own stop, from its own entry


# ---- B4: fills vs quotes ---------------------------------------------------------

def test_tca_reports_live_slippage_against_the_assumed(client, isolated_db):
    from app.db import session as dbs
    from app.db.models import AlgoStrategy, AlgoTrade
    from app.algo import runner as rn

    with dbs.SessionLocal() as db:
        s = AlgoStrategy(name="tca", spec=_opt_spec(), enabled=False, mode="live")
        db.add(s)
        db.flush()
        at = rn._utc(__import__("time").time() - 3600).replace(tzinfo=None)
        for mode in ("live", "paper"):
            db.add(AlgoTrade(strategy_id=s.id, symbol=NIFTY, side="BUY", quantity=65, mode=mode, status="closed",
                             entry_at=at, legs=[
                                 {"kind": "CE", "act": 1, "qty": 65, "entry_quote": 100.0, "entry": 101.0,       # paid 1% more
                                  "exit_quote": 120.0, "exit": 118.8},                                         # sold 1% lower
                                 {"kind": "PE", "act": -1, "qty": 65, "entry_quote": 80.0, "entry": 80.0,
                                  "exit_quote": 70.0, "exit": 70.7}]))                                          # bought back 1% higher
        db.commit()
    r = client.get("/api/algo/tca?days=5").json()["rows"]
    ce = next(x for x in r if x["kind"] == "CE")
    pe = next(x for x in r if x["kind"] == "PE")
    assert ce["fills"] == 2 and ce["median_pct"] == pytest.approx(1.0) and ce["rupees"] == pytest.approx(65 + 78)
    assert pe["fills"] == 2 and pe["median_pct"] == pytest.approx(0.5) and pe["assumed_pct"] == 0   # paper rows ignored


# ---- A0: fixes ported from the parallel Half B ------------------------------------

def test_mtm_percent_stays_on_the_original_premium():
    legs = [{"kind": "CE", "act": -1, "entry": 100.0, "qty": 65}, {"kind": "PE", "act": -1, "entry": 90.0, "qty": 65},
            {"kind": "CE", "act": -1, "entry": 150.0, "qty": 65, "via": "REENTRY"}]
    assert engine.premium_base(legs) == pytest.approx(190 * 65)                 # the re-entry doesn't move the base


def test_hedge_is_sized_on_the_total_delta():
    spec = _opt_spec(instrument=_strangle(), position_rules={"hedge": {"above": 0.3, "max": 5}})
    fut = lambda per_set, sets: [{"kind": "FUT", "act": -1, "per_set": per_set, "qty": per_set * sets, "entry": 24500.0}]  # noqa: E731
    lots = lambda legs: [a[1] for a in engine.adjustments(spec, "BUY", legs, 24500.0, DAY0, 65, lambda lg: 24500.0, 0.14)  # noqa: E731
                         if a[0] == "hedge"]
    assert lots(fut(39, 2)) == [1]            # -0.6 per set x 2 sets = -1.2 lots -> buy 1
    assert lots(fut(39, 1)) == [1]            # -0.6 -> 1 (half away from zero)
    assert lots(fut(26, 2)) == [1]            # -0.4 x 2 = -0.8 -> 1: per-set rounding would never hedge this
    assert lots(fut(26, 1)) == []             # -0.4 rounds to 0
    assert lots(fut(65, 3)) == [3]


def test_reentries_need_a_stop_and_adjusted_levels_sit_on_the_underlying():
    with pytest.raises(ValueError, match="needs its own stop"):
        _opt_spec(instrument=_strangle(reentries=1))
    assert _opt_spec(instrument=_strangle("cost", reentries=1))["instrument"]["legs_long"][0]["reentries"] == 1
    with pytest.raises(ValueError, match="levels_on 'underlying'"):
        _opt_spec(instrument={"type": "option", "levels_on": "instrument", "iv": {"source": "fixed", "value": 14},
                              "legs_long": [{"right": "CE", "action": "SELL", "strike": "ATM", "lots": 1,
                                             "roll": {"above": 0.6, "to": 0.3}}]},
                  stop_loss={"type": "pct", "value": 30})


def test_skew_reaches_span_margin_and_added_legs_must_fit_the_capital():
    leg = {"kind": "PE", "act": -1, "K": 24000.0, "exp": DAY0 + 6 * 86400, "per_set": 65, "qty": 65, "entry": 50.0}
    flat = fno.span_margin([leg], 24500.0, DAY0, name="NIFTY", iv=0.14)["total"]
    skewed = fno.span_margin([{**leg, "skew": (0.8, 0.0)}], 24500.0, DAY0, name="NIFTY", iv=0.14)["total"]
    assert skewed != pytest.approx(flat)                                       # SPAN prices the leg at its skewed IV
    tight = engine.run(_opt_spec(instrument=_strangle(), position_rules={"hedge": {"above": 0.5, "max": 3}},
                                 margin={"model": "span"}, portfolio={"capital": 300_000, "leverage": 8}),
                       {"N": _nifty(_RISE_FALL)})
    assert tight["stats"]["skipped"].get("hedge_no_capital", 0) >= 1           # no room for the future: counted, not taken
    assert not any(lg.get("via") == "HEDGE" for t in tight["trades"] for lg in t["legs"])


# ---- a carried position meets its expiry ---------------------------------------

def _carried_week(after_expiry_step: float) -> dict:
    """NIFTY 5m candles Mon 19 Oct .. Wed 28 Oct 2026 (weekly expiries Tue 20 and
    Tue 27 Oct): a cross above 24550 on Monday, then flat, then from Wednesday on
    a steady move of `after_expiry_step` points a candle."""
    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    t, o, h, l, c = [], [], [], [], []
    px = 24500.0
    for k, dd in enumerate((19, 20, 21, 22, 23, 26, 27, 28)):
        day0 = int(datetime(2026, 10, dd, 9, 15, tzinfo=ist).timestamp())
        for m in range(75):
            nxt = 24560.0 if (k, m) == (0, 3) else px + after_expiry_step if k >= 2 else px
            t.append(day0 + m * 300); o.append(px); c.append(nxt); h.append(max(px, nxt) + 2); l.append(min(px, nxt) - 2)
            px = nxt
    return {"t": t, "o": o, "h": h, "l": l, "c": c, "v": [0] * len(t), "tf_s": 300,
            "meta": {"symbol": NIFTY, "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}}


def _sold_call(**kw):
    """A carried (square-off off) sold weekly 4-OTM NIFTY call."""
    return _opt_spec(**{"instrument": {"type": "option", "expiry_kind": "weekly", "iv": {"source": "fixed", "value": 14},
                                       "legs_long": [{"right": "CE", "action": "SELL", "strike": "OTM", "steps": 4, "lots": 1}]},
                        "sizing": {"mode": "lots", "value": 1},
                        "session": {"start": "09:15", "end": None, "square_off": None},
                        "costs": {"slippage_pct": 0, "charges": False}, **kw})


def test_carried_option_exits_at_15_15_on_its_expiry_day():
    # Square-off off: a sold weekly 24750 CE is carried overnight. On its expiry day
    # (Tue 20 Oct) it is bought back at 15:15 — the latest live square-off, before the
    # exchange settles it — and the rally from Wednesday on never touches it.
    d = _carried_week(6.0)
    (t,) = engine.run(_sold_call(), {"N": d})["trades"]
    expiry = fno.expiry_after("NIFTY", "NSE", "weekly", "current", t["entry_t"])
    assert t["reason"] == "EXPIRY" and t["exit_t"] == expiry - 15 * 60          # Tue 20 Oct 15:15
    assert t["legs"][0]["exit"] == 0.05                                          # 15 minutes before a worthless expiry
    assert t["net"] == pytest.approx((t["legs"][0]["entry"] - 0.05) * 65)


def test_carried_future_exits_on_its_expiry_day():
    spec = _opt_spec(instrument={"type": "future", "expiry": "current"}, sizing={"mode": "lots", "value": 1},
                     session={"start": "09:15", "end": "15:00", "square_off": None},
                     costs={"slippage_pct": 0, "charges": False})
    d = {**_carried_week(0.0), "meta": {"symbol": NIFTY, "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}}
    tr = engine.run(spec, {"N": d})["trades"]
    # the monthly future (Tue 27 Oct) is closed at 15:15 that day, not at the end of the data
    expiry = fno.expiry_after("NIFTY", "NSE", "monthly", "current", tr[0]["entry_t"])
    assert tr[0]["reason"] == "EXPIRY" and tr[0]["exit_t"] == expiry - 15 * 60


def test_a_carried_position_past_its_expiry_is_settled_at_intrinsic():
    # A data gap: the expiry day's candles stop at 14:00, so there is no 15:15 candle to
    # exit on. The call is still settled — on the last candle before the expiry, at its
    # intrinsic value (worthless here), no slippage — never carried into the next week.
    from datetime import datetime, timedelta, timezone

    ist = timezone(timedelta(hours=5, minutes=30))
    gap_from = int(datetime(2026, 10, 20, 14, 0, tzinfo=ist).timestamp())     # Tue (expiry day) 14:00
    gap_to = int(datetime(2026, 10, 21, 0, 0, tzinfo=ist).timestamp())
    d = _carried_week(6.0)
    keep = [k for k, t in enumerate(d["t"]) if not gap_from <= t < gap_to]
    d = {k: ([v[j] for j in keep] if isinstance(v, list) else v) for k, v in d.items()}
    (t,) = engine.run(_sold_call(costs={"slippage_pct": 0.5, "charges": False}), {"N": d})["trades"]
    assert t["reason"] == "EXPIRY" and t["exit_t"] == gap_from                # the 13:55 candle's close
    assert t["legs"][0]["exit"] == 0.05 and t["legs"][0]["reason"] == "EXPIRY"   # settled: no 0.5% slippage


def test_a_carried_strategy_opens_nothing_expiring_that_day_after_15_15():
    # The weekly expires Tue 20 Oct. A signal whose fill comes at 15:15 that day would be
    # sold and bought back at once — it is skipped; one filled at 15:10 is closed at 15:15.
    from datetime import datetime, timedelta, timezone

    day0 = int(datetime(2026, 10, 20, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30))).timestamp())

    def tuesday(signal_bar: int) -> dict:
        c = [24500.0] * 75
        for m in range(signal_bar, 75):
            c[m] = 24560.0                                   # crosses above 24550 on the signal bar
        d = bars(c, spread=5)
        d["t"] = [day0 + m * 300 for m in range(75)]
        d["meta"] = {"symbol": NIFTY, "name": "NIFTY", "exch": "NSE", "lot": 65, "step": 50}
        return d

    late = engine.run(_sold_call(), {"N": tuesday(71)})       # signal 15:10-15:15, fill 15:15
    assert late["trades"] == [] and late["stats"]["skipped"]["expires_today"] == 1
    (t,) = engine.run(_sold_call(), {"N": tuesday(70)})["trades"]   # fill 15:10
    assert (t["reason"], t["exit_t"] - t["entry_t"]) == ("EXPIRY", 300)
